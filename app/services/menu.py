"""Menu-management business logic: categories, menu items, ingredients.

No Flask request/session here, same rule as app/services/auth.py -- callers
pass plain values and get plain objects back. Every enumerated field
(cuisine, spice level, dietary type) is re-validated against its enum here
even though the form layer already constrains it via a <select>: a raw POST
can always skip the form.

Audit logging and "what changed" detection live in the route layer
(app/routes/admin_menu.py), which already holds the pre-update object and
is the natural place to know which specific audit event(s) a change
implies -- these functions just validate, mutate, and return.

M5 adds display order (``sort_order``), per-locale translations of the
customer-facing text, ``public_menu_query`` -- the one definition of "on the
public menu" (available item in an active category) that the menu, cart,
checkout and recommendations all build on -- and ``set_availability``, the
STAFF/ADMIN sold-out switch that writes ``is_available`` and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.category import SORT_ORDER_MAX, Category, CategoryTranslation
from app.models.enums import Cuisine, DietaryType, Locale, SpiceLevel
from app.models.ingredient import Ingredient
from app.models.menu_item import MenuItem, MenuItemTranslation
from app.models.user import Role, User
from app.services.audit import commit_with
from app.services.errors import ValidationError

MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("100000.00")

MAX_INGREDIENT_NAME_LENGTH = 80


class CategoryError(ValidationError):
    pass


class MenuItemError(ValidationError):
    pass


# --- shared: display order and translations ------------------------------------------

# Deterministic everywhere: explicit order, then name, then id.
CATEGORY_ORDER = (Category.sort_order, Category.name, Category.id)
ITEM_ORDER = (MenuItem.sort_order, MenuItem.name, MenuItem.id)

_CONCURRENT_EDIT = "Someone else changed this at the same time. Reload and try again."


def _validate_sort_order(raw, errors: dict[str, list[str]]) -> int:
    try:
        value = int(raw if raw is not None else 0)
    except (TypeError, ValueError):
        value = -1
    if not 0 <= value <= SORT_ORDER_MAX:
        errors["sort_order"] = [f"Display order must be a whole number from 0 to {SORT_ORDER_MAX}."]
    return value


def _clean_translations(raw: dict | None, name_max: int, description_max: int,
                        errors: dict[str, list[str]]) -> dict | None:
    """``{Locale: (name, description) | None}`` from ``{"ne": (name,
    description)}``, validated. A blank name means "no translation for this
    locale" (``None``: the row is removed, so the canonical text shows); a
    description without a name is refused."""
    if raw is None:
        return None
    cleaned = {}
    for code, (name, description) in raw.items():
        try:
            locale = Locale(code)
        except ValueError:
            errors.setdefault("translations", []).append("Unsupported language.")
            continue
        name = (name or "").strip()
        description = (description or "").strip() or None
        if not name:
            if description:
                errors.setdefault(f"name_{locale.value}", []).append(
                    "Enter the translated name as well as the description.")
            cleaned[locale] = None
            continue
        if len(name) > name_max:
            errors.setdefault(f"name_{locale.value}", []).append(f"Name must be at most {name_max} characters.")
        if description and len(description) > description_max:
            errors.setdefault(f"description_{locale.value}", []).append(
                f"Description must be at most {description_max} characters.")
        cleaned[locale] = (name, description)
    return cleaned


def _apply_translations(entity, model, wanted: dict | None) -> None:
    """Apply ``wanted`` to ``entity.translations``, touching only the locales
    it names: a value sets that locale's row, ``None`` removes it. Locales it
    does not name are left alone (``wanted=None`` changes nothing)."""
    if wanted is None:
        return
    existing = {t.locale: t for t in entity.translations}
    for locale, value in wanted.items():
        row = existing.get(locale)
        if value is None:
            if row is not None:
                entity.translations.remove(row)
            continue
        name, description = value
        if row is None:
            entity.translations.append(model(locale=locale, name=name, description=description))
        else:
            row.name, row.description = name, description


# --- categories --------------------------------------------------------


@dataclass
class CategoryInput:
    name: str
    description: str | None
    is_active: bool = True
    sort_order: int = 0
    # {"ne": (name, description)}; None leaves translations unchanged.
    translations: dict | None = None


def _validate_category_name(name: str) -> list[str]:
    errors = []
    if not name:
        errors.append("Name is required.")
    elif len(name) > 80:
        errors.append("Name must be at most 80 characters.")
    return errors


def _category_name_taken(name: str, *, exclude_id: int | None = None) -> bool:
    query = db.session.query(Category.id).filter(Category.name == name)
    if exclude_id is not None:
        query = query.filter(Category.id != exclude_id)
    return query.first() is not None


def create_category(data: CategoryInput, *, audit=None) -> Category:
    name = data.name.strip()
    errors: dict[str, list[str]] = {}

    if name_errors := _validate_category_name(name):
        errors["name"] = name_errors
    elif _category_name_taken(name):
        errors["name"] = ["A category with this name already exists."]
    sort_order = _validate_sort_order(data.sort_order, errors)
    translations = _clean_translations(data.translations, 80, 500, errors)

    if errors:
        raise CategoryError(errors)

    category = Category(name=name, description=(data.description or "").strip() or None, is_active=data.is_active,
                        sort_order=sort_order)
    _apply_translations(category, CategoryTranslation, translations)
    db.session.add(category)
    try:
        commit_with(audit, category)
    except IntegrityError:
        db.session.rollback()
        raise CategoryError({"name": ["A category with this name already exists."]})
    return category


def update_category(category: Category, data: CategoryInput, *, audit=None) -> Category:
    name = data.name.strip()
    errors: dict[str, list[str]] = {}

    if name_errors := _validate_category_name(name):
        errors["name"] = name_errors
    elif _category_name_taken(name, exclude_id=category.id):
        errors["name"] = ["A category with this name already exists."]
    sort_order = _validate_sort_order(data.sort_order, errors)
    translations = _clean_translations(data.translations, 80, 500, errors)

    if errors:
        raise CategoryError(errors)

    category.name = name
    category.description = (data.description or "").strip() or None
    category.is_active = data.is_active
    category.sort_order = sort_order
    _apply_translations(category, CategoryTranslation, translations)

    try:
        commit_with(audit, category)
    except IntegrityError:
        # The name's unique index, or a translation for the same locale
        # added by a concurrent edit.
        db.session.rollback()
        raise CategoryError({"name": ["A category with this name already exists, or " + _CONCURRENT_EDIT.lower()]})
    return category


def list_admin_categories() -> list[Category]:
    return db.session.query(Category).options(selectinload(Category.translations)).order_by(*CATEGORY_ORDER).all()


def list_public_categories() -> list[Category]:
    return (db.session.query(Category).options(selectinload(Category.translations))
            .filter_by(is_active=True).order_by(*CATEGORY_ORDER).all())


# --- menu items ----------------------------------------------------------


@dataclass
class MenuItemInput:
    category_id: int
    name: str
    description: str | None
    price: str  # raw form input; parsed and validated here, not trusted as-is
    cuisine: str
    spice_level: str
    dietary_type: str
    is_available: bool = True
    sort_order: int = 0
    # {"ne": (name, description)}; None leaves translations unchanged.
    translations: dict | None = None
    # ``is_available`` above is used by create_menu_item only. After that,
    # availability changes only through set_availability (M5).


def _validate_price(raw: str) -> tuple[Decimal | None, list[str]]:
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None, ["Price must be a valid number."]

    errors = []
    if price < MIN_PRICE:
        errors.append(f"Price must be at least {MIN_PRICE}.")
    if price > MAX_PRICE:
        errors.append(f"Price must be at most {MAX_PRICE}.")
    return price.quantize(Decimal("0.01")), errors


def _coerce_enum(enum_cls, value: str, field_name: str, errors: dict[str, list[str]]):
    try:
        return enum_cls(value)
    except ValueError:
        errors.setdefault(field_name, []).append(f"Invalid {field_name.replace('_', ' ')}.")
        return None


def _validate_menu_item_input(data: MenuItemInput):
    errors: dict[str, list[str]] = {}

    name = data.name.strip()
    if not name:
        errors.setdefault("name", []).append("Name is required.")
    elif len(name) > 120:
        errors.setdefault("name", []).append("Name must be at most 120 characters.")

    description = (data.description or "").strip() or None
    if description and len(description) > 2000:
        errors.setdefault("description", []).append("Description must be at most 2000 characters.")

    category = db.session.get(Category, data.category_id) if data.category_id else None
    if category is None:
        errors.setdefault("category_id", []).append("Select a valid category.")

    price, price_errors = _validate_price(data.price)
    if price_errors:
        errors["price"] = price_errors

    cuisine = _coerce_enum(Cuisine, data.cuisine, "cuisine", errors)
    spice_level = _coerce_enum(SpiceLevel, data.spice_level, "spice_level", errors)
    dietary_type = _coerce_enum(DietaryType, data.dietary_type, "dietary_type", errors)
    sort_order = _validate_sort_order(data.sort_order, errors)
    translations = _clean_translations(data.translations, 120, 2000, errors)

    if errors:
        raise MenuItemError(errors)

    return name, description, category, price, cuisine, spice_level, dietary_type, sort_order, translations


def _commit_menu_item(item: MenuItem, audit) -> None:
    try:
        commit_with(audit, item)
    except IntegrityError:
        # Only a translation for the same locale added by a concurrent edit
        # can collide here (menu items have no other unique column).
        db.session.rollback()
        raise MenuItemError({"translations": [_CONCURRENT_EDIT]})


def create_menu_item(data: MenuItemInput, *, audit=None) -> MenuItem:
    (name, description, category, price, cuisine, spice_level, dietary_type, sort_order,
     translations) = _validate_menu_item_input(data)

    item = MenuItem(
        category_id=category.id,
        name=name,
        description=description,
        price=price,
        cuisine=cuisine,
        spice_level=spice_level,
        dietary_type=dietary_type,
        is_available=data.is_available,
        sort_order=sort_order,
    )
    _apply_translations(item, MenuItemTranslation, translations)
    db.session.add(item)
    _commit_menu_item(item, audit)
    return item


def update_menu_item(item: MenuItem, data: MenuItemInput, *, audit=None) -> MenuItem:
    """Edit an item's details. Never writes ``is_available`` -- not even the
    value it was loaded with -- so an edit form opened before a sold-out
    change cannot undo it; availability changes only via ``set_availability``."""
    (name, description, category, price, cuisine, spice_level, dietary_type, sort_order,
     translations) = _validate_menu_item_input(data)

    item.category_id = category.id
    item.name = name
    item.description = description
    item.price = price
    item.cuisine = cuisine
    item.spice_level = spice_level
    item.dietary_type = dietary_type
    item.sort_order = sort_order
    _apply_translations(item, MenuItemTranslation, translations)
    _commit_menu_item(item, audit)
    return item


# Customer-facing lists render each item's (and its category's) translations;
# load them in two batched queries instead of one per row.
_WITH_TRANSLATIONS = (
    selectinload(MenuItem.translations),
    selectinload(MenuItem.category).selectinload(Category.translations),
)


def list_admin_menu_items() -> list[MenuItem]:
    return (db.session.query(MenuItem).join(Category).options(*_WITH_TRANSLATIONS)
            .order_by(*CATEGORY_ORDER, *ITEM_ORDER).all())


def public_menu_query():
    """The single definition of "on the public menu": an available item in an
    active category. Every customer-facing read of menu items -- the menu,
    the cart, checkout and recommendations -- starts here. Never accepts a
    price or availability override from the caller; there is none to accept."""
    return (
        db.session.query(MenuItem)
        .join(Category)
        .filter(MenuItem.is_available.is_(True), Category.is_active.is_(True))
    )


def list_public_menu_items(category_id: int | None = None) -> list[MenuItem]:
    query = public_menu_query().options(*_WITH_TRANSLATIONS)
    if category_id is not None:
        query = query.filter(MenuItem.category_id == category_id)
    return query.order_by(*CATEGORY_ORDER, *ITEM_ORDER).all()


def get_public_menu_item(item_id: int) -> MenuItem | None:
    return public_menu_query().options(*_WITH_TRANSLATIONS).filter(MenuItem.id == item_id).first()


# --- sold out / available (STAFF and ADMIN) --------------------------------------------


def set_availability(actor: User, item_id: int, available: bool, *, audit=None) -> bool:
    """Mark an item available or sold out. Returns False when it already was
    (nothing written, nothing audited).

    One conditional UPDATE of ``is_available`` alone: it never rewrites the
    item's other columns, so it cannot undo a concurrent admin edit, and two
    staff setting the same state at once produce one change and one audit
    row. The target state is explicit (not a toggle), so a repeated or
    duplicated request cannot flip it back."""
    if actor.role not in (Role.STAFF, Role.ADMIN):
        raise MenuItemError({"is_available": ["Only staff can change availability."]})
    changed = db.session.execute(
        sa.update(MenuItem)
        .where(MenuItem.id == item_id, MenuItem.is_available.is_(not available))
        .values(is_available=available)
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    if not changed:
        db.session.rollback()
        return False
    commit_with(audit, item_id, available)
    return True


# --- ingredients -----------------------------------------------------------


def parse_ingredient_names(raw: str) -> list[str]:
    """Comma-separated field -> normalized, deduplicated, order-preserved
    names. ``""`` / whitespace-only entries are dropped."""
    names: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        normalized = part.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            names.append(normalized)
    return names


def sync_menu_item_ingredients(item: MenuItem, names: list[str], *, audit=None) -> bool:
    """Set item.ingredients to exactly ``names`` (find-or-create each
    Ingredient by normalized name). Returns True if the set changed."""
    too_long = [n for n in names if len(n) > MAX_INGREDIENT_NAME_LENGTH]
    if too_long:
        raise MenuItemError(
            {"ingredients": [f"Ingredient name too long (max {MAX_INGREDIENT_NAME_LENGTH} chars): {too_long[0]!r}"]}
        )

    current = {ing.name for ing in item.ingredients}
    target = set(names)
    if current == target:
        return False

    existing = {ing.name: ing for ing in db.session.query(Ingredient).filter(Ingredient.name.in_(names)).all()}
    resolved = []
    for name in names:
        ingredient = existing.get(name)
        if ingredient is None:
            ingredient = Ingredient(name=name)
            db.session.add(ingredient)
            existing[name] = ingredient
        resolved.append(ingredient)

    item.ingredients = resolved
    commit_with(audit, item)
    return True
