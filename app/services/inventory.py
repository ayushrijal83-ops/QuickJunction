"""Inventory (M13): ingredients as stock, recipes, and the movement ledger.

The rule this module exists to keep: **an ingredient's quantity never
changes without a StockMovement row**, written in the same transaction by
``_move``, which updates the quantity with one atomic ``UPDATE … SET
current_quantity = current_quantity + :delta`` (safe under concurrency) and
records the resulting level.

Sales: ``deduct_for_order`` is called by the order service inside the
``→ COMPLETED`` transition's transaction, so the completion and its stock
deduction commit together. It is idempotent -- ``uq_stock_movements_order_
ingredient`` rejects a second SALE row for the same order and ingredient,
and the function skips work already done. Cancelled orders never reach it,
and a completed order can never be cancelled.

Who may do what (routes enforce it with ``require_role``; the service checks
again for ADJUSTMENT): STAFF and ADMIN view inventory and record PURCHASE /
RESTOCK / WASTE; only ADMIN records ADJUSTMENT, edits ingredients and
recipes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.ingredient import Ingredient, StockUnit
from app.models.menu_item import MenuItem
from app.models.menu_item_ingredient import MenuItemIngredient
from app.models.order import Order
from app.models.stock_movement import MovementType, StockMovement
from app.models.user import Role, User
from app.services.errors import ValidationError

_QTY = Decimal("0.001")
MAX_QUANTITY = Decimal("1000000")
MANUAL_TYPES = (MovementType.PURCHASE, MovementType.RESTOCK, MovementType.WASTE, MovementType.ADJUSTMENT)


class InventoryError(ValidationError):
    pass


def _fail(field: str, message: str):
    raise InventoryError({field: [message]})


def _quantity(raw, field: str, *, allow_negative=False, allow_zero=False) -> Decimal:
    try:
        value = Decimal(str(raw).strip()).quantize(_QTY)
    except (InvalidOperation, ValueError):
        _fail(field, "Enter a number.")
    if not value.is_finite() or abs(value) > MAX_QUANTITY:
        _fail(field, "Enter a sensible quantity.")
    if (value < 0 and not allow_negative) or (value == 0 and not allow_zero):
        _fail(field, "Enter a quantity greater than zero." if not allow_negative else "Enter a non-zero quantity.")
    return value


# --- the one writer ------------------------------------------------------------------


def _move(ingredient_id: int, delta: Decimal, movement_type: MovementType, *, actor_id: int | None,
          order_id: int | None = None, note: str | None = None) -> StockMovement:
    """Apply ``delta`` and record it -- no commit (callers own the transaction)."""
    db.session.execute(
        sa.update(Ingredient).where(Ingredient.id == ingredient_id)
        .values(current_quantity=Ingredient.current_quantity + delta)
        .execution_options(synchronize_session=False)
    )
    after = db.session.execute(
        sa.select(Ingredient.current_quantity).where(Ingredient.id == ingredient_id)
    ).scalar_one()
    movement = StockMovement(ingredient_id=ingredient_id, movement_type=movement_type, quantity_change=delta,
                             quantity_after=after, order_id=order_id, actor_id=actor_id,
                             note=(note or "").strip()[:255] or None)
    db.session.add(movement)
    return movement


# --- ingredients ---------------------------------------------------------------------


def save_ingredient(ingredient: Ingredient | None, name, unit, minimum, is_active: bool) -> Ingredient:
    """Create or edit an ingredient's definition. Never touches its quantity
    -- opening stock is a PURCHASE movement, so it is on the ledger too."""
    name = (name or "").strip().lower()
    if not name or len(name) > 80:
        _fail("name", "Name is required (max 80 characters).")
    try:
        unit = StockUnit(unit)
    except ValueError:
        _fail("unit", "Choose a unit.")
    minimum = _quantity(minimum, "minimum_quantity", allow_zero=True)
    ingredient = ingredient or Ingredient()
    ingredient.name, ingredient.unit, ingredient.minimum_quantity, ingredient.is_active = name, unit, minimum, is_active
    db.session.add(ingredient)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _fail("name", f"An ingredient called {name!r} already exists.")
    return ingredient


def record_movement(ingredient: Ingredient, raw_type, raw_quantity, note: str | None, actor: User) -> StockMovement:
    """A manual movement. PURCHASE/RESTOCK add, WASTE removes (the quantity
    is entered as a positive amount), ADJUSTMENT is a signed correction and
    is ADMIN only."""
    try:
        movement_type = MovementType(raw_type)
    except ValueError:
        _fail("movement_type", "Choose a movement type.")
    if movement_type not in MANUAL_TYPES:
        _fail("movement_type", "Sales are recorded automatically when an order completes.")
    if movement_type == MovementType.ADJUSTMENT and actor.role != Role.ADMIN:
        _fail("movement_type", "Only an admin can adjust stock counts.")
    if movement_type in (MovementType.WASTE, MovementType.ADJUSTMENT) and not (note or "").strip():
        _fail("note", "Say why stock is being written off or corrected.")
    signed = movement_type == MovementType.ADJUSTMENT
    quantity = _quantity(raw_quantity, "quantity", allow_negative=signed)
    delta = -quantity if movement_type == MovementType.WASTE else quantity
    movement = _move(ingredient.id, delta, movement_type, actor_id=actor.id, note=note)
    db.session.commit()
    db.session.refresh(ingredient)
    return movement


# --- recipes ---------------------------------------------------------------------------


def set_recipe(item: MenuItem, quantities: dict[int, str]) -> None:
    """Set per-portion quantities for ingredients already linked to ``item``
    (links come from the menu form). 0 = listed but not stock-tracked."""
    links = {link.ingredient_id: link for link in
             db.session.query(MenuItemIngredient).filter_by(menu_item_id=item.id)}
    for ingredient_id, raw in quantities.items():
        link = links.get(ingredient_id)
        if link is None:
            _fail("recipe", "That ingredient is not on this menu item.")
        link.quantity = _quantity(raw, "recipe", allow_zero=True)
    db.session.commit()


def add_to_recipe(item: MenuItem, ingredient: Ingredient, raw_quantity) -> None:
    quantity = _quantity(raw_quantity, "recipe")
    link = db.session.get(MenuItemIngredient, (item.id, ingredient.id))
    if link is None:
        db.session.add(MenuItemIngredient(menu_item_id=item.id, ingredient_id=ingredient.id, quantity=quantity))
    else:
        link.quantity = quantity
    db.session.commit()


def recipe_requirements(lines: dict[int, int]) -> dict[int, Decimal]:
    """{ingredient_id: total quantity} needed for {menu_item_id: portions},
    counting only stock-tracked (active, quantity > 0) recipe lines."""
    if not lines:
        return {}
    rows = db.session.execute(
        sa.select(MenuItemIngredient.menu_item_id, MenuItemIngredient.ingredient_id, MenuItemIngredient.quantity)
        .join(Ingredient, Ingredient.id == MenuItemIngredient.ingredient_id)
        .where(MenuItemIngredient.menu_item_id.in_(lines), MenuItemIngredient.quantity > 0, Ingredient.is_active)
    ).all()
    need: dict[int, Decimal] = defaultdict(Decimal)
    for menu_item_id, ingredient_id, per_portion in rows:
        need[ingredient_id] += per_portion * lines[menu_item_id]
    return dict(need)


def shortages(lines: dict[int, int]) -> list[str]:
    """Names of ingredients a cart would need more of than is in stock --
    checked at checkout so customers cannot order what cannot be made.
    ponytail: not a reservation; two carts can both pass and the later
    completion drives stock negative (shown as "short"), which is the
    intended trade-off over blocking a served order."""
    need = recipe_requirements(lines)
    if not need:
        return []
    stock = db.session.query(Ingredient).filter(Ingredient.id.in_(need)).all()
    return sorted(i.name for i in stock if need[i.id] > i.current_quantity)


# --- sales -----------------------------------------------------------------------------


def deduct_for_order(order_id: int, actor_id: int | None) -> int:
    """SALE movements for a completed order, inside the caller's transaction.
    Returns the number of ingredients deducted; 0 if already done."""
    already = set(db.session.scalars(
        sa.select(StockMovement.ingredient_id).where(StockMovement.order_id == order_id)
    ))
    portions: dict[int, int] = defaultdict(int)
    order = db.session.get(Order, order_id)
    for line in order.items:
        portions[line.menu_item_id] += line.quantity
    count = 0
    for ingredient_id, quantity in sorted(recipe_requirements(portions).items()):
        if ingredient_id in already:
            continue
        _move(ingredient_id, -quantity, MovementType.SALE, actor_id=actor_id, order_id=order_id,
              note=f"Order #{order_id}")
        count += 1
    return count


# --- reading -------------------------------------------------------------------------


def list_ingredients() -> list[Ingredient]:
    return db.session.query(Ingredient).order_by(Ingredient.is_active.desc(), Ingredient.name).all()


def movement_totals(since: date) -> dict[int, dict[MovementType, Decimal]]:
    """{ingredient_id: {type: Σ |change|}} since ``since`` -- usage (SALE),
    wastage, purchases/restocks and adjustments per ingredient."""
    rows = db.session.execute(
        sa.select(StockMovement.ingredient_id, StockMovement.movement_type,
                  sa.func.sum(StockMovement.quantity_change))
        .where(StockMovement.created_at >= datetime.combine(since, datetime.min.time()))
        .group_by(StockMovement.ingredient_id, StockMovement.movement_type)
    ).all()
    totals: dict[int, dict[MovementType, Decimal]] = defaultdict(dict)
    for ingredient_id, movement_type, total in rows:
        totals[ingredient_id][movement_type] = Decimal(total)
    return totals


def history(ingredient: Ingredient, limit: int = 200) -> list[StockMovement]:
    return (
        db.session.query(StockMovement).options(selectinload(StockMovement.actor))
        .filter_by(ingredient_id=ingredient.id)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(limit).all()
    )


def recipe_lines(item: MenuItem) -> list[MenuItemIngredient]:
    return (
        db.session.query(MenuItemIngredient).options(selectinload(MenuItemIngredient.ingredient))
        .filter_by(menu_item_id=item.id).all()
    )


def since_days(days: int, today: date) -> date:
    return today - timedelta(days=days - 1)
