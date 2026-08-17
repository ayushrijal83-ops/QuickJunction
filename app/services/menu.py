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
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.category import Category
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.models.ingredient import Ingredient
from app.models.menu_item import MenuItem
from app.services.errors import ValidationError

MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("100000.00")

MAX_INGREDIENT_NAME_LENGTH = 80


class CategoryError(ValidationError):
    pass


class MenuItemError(ValidationError):
    pass


# --- categories --------------------------------------------------------


@dataclass
class CategoryInput:
    name: str
    description: str | None
    is_active: bool = True


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


def create_category(data: CategoryInput) -> Category:
    name = data.name.strip()
    errors: dict[str, list[str]] = {}

    if name_errors := _validate_category_name(name):
        errors["name"] = name_errors
    elif _category_name_taken(name):
        errors["name"] = ["A category with this name already exists."]

    if errors:
        raise CategoryError(errors)

    category = Category(name=name, description=(data.description or "").strip() or None, is_active=data.is_active)
    db.session.add(category)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise CategoryError({"name": ["A category with this name already exists."]})
    return category


def update_category(category: Category, data: CategoryInput) -> Category:
    name = data.name.strip()
    errors: dict[str, list[str]] = {}

    if name_errors := _validate_category_name(name):
        errors["name"] = name_errors
    elif _category_name_taken(name, exclude_id=category.id):
        errors["name"] = ["A category with this name already exists."]

    if errors:
        raise CategoryError(errors)

    category.name = name
    category.description = (data.description or "").strip() or None
    category.is_active = data.is_active

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise CategoryError({"name": ["A category with this name already exists."]})
    return category


def list_admin_categories() -> list[Category]:
    return db.session.query(Category).order_by(Category.name).all()


def list_public_categories() -> list[Category]:
    return db.session.query(Category).filter_by(is_active=True).order_by(Category.name).all()


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

    if errors:
        raise MenuItemError(errors)

    return name, description, category, price, cuisine, spice_level, dietary_type


def create_menu_item(data: MenuItemInput) -> MenuItem:
    name, description, category, price, cuisine, spice_level, dietary_type = _validate_menu_item_input(data)

    item = MenuItem(
        category_id=category.id,
        name=name,
        description=description,
        price=price,
        cuisine=cuisine,
        spice_level=spice_level,
        dietary_type=dietary_type,
        is_available=data.is_available,
    )
    db.session.add(item)
    db.session.commit()
    return item


def update_menu_item(item: MenuItem, data: MenuItemInput) -> MenuItem:
    name, description, category, price, cuisine, spice_level, dietary_type = _validate_menu_item_input(data)

    item.category_id = category.id
    item.name = name
    item.description = description
    item.price = price
    item.cuisine = cuisine
    item.spice_level = spice_level
    item.dietary_type = dietary_type
    item.is_available = data.is_available
    db.session.commit()
    return item


def list_admin_menu_items() -> list[MenuItem]:
    return db.session.query(MenuItem).order_by(MenuItem.name).all()


def list_public_menu_items(category_id: int | None = None) -> list[MenuItem]:
    """Active category, available item, only -- the one filter every public
    menu query goes through. Never accepts a price or availability
    override from the caller; there is none to accept."""
    query = (
        db.session.query(MenuItem)
        .join(Category)
        .filter(MenuItem.is_available.is_(True), Category.is_active.is_(True))
    )
    if category_id is not None:
        query = query.filter(MenuItem.category_id == category_id)
    return query.order_by(MenuItem.name).all()


def get_public_menu_item(item_id: int) -> MenuItem | None:
    return (
        db.session.query(MenuItem)
        .join(Category)
        .filter(MenuItem.id == item_id, MenuItem.is_available.is_(True), Category.is_active.is_(True))
        .first()
    )


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


def sync_menu_item_ingredients(item: MenuItem, names: list[str]) -> bool:
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
    db.session.commit()
    return True
