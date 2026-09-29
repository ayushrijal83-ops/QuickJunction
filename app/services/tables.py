"""Restaurant table management.

Table status is operational state with an explicit transition allow-list,
the same pattern as ``ALLOWED_STATUS_TRANSITIONS`` for orders. No function
here ever touches an order: freeing a table leaves every historical order's
``table_id`` exactly where it was.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.order import Order, OrderStatus
from app.models.restaurant_table import MAX_CAPACITY, MIN_CAPACITY, RestaurantTable, TableStatus
from app.services.errors import ValidationError

S = TableStatus
ALLOWED_TABLE_TRANSITIONS: dict[TableStatus, frozenset[TableStatus]] = {
    S.AVAILABLE: frozenset({S.OCCUPIED, S.RESERVED, S.OUT_OF_SERVICE}),
    S.RESERVED: frozenset({S.OCCUPIED, S.AVAILABLE, S.OUT_OF_SERVICE}),
    S.OCCUPIED: frozenset({S.CLEANING, S.AVAILABLE}),
    S.CLEANING: frozenset({S.AVAILABLE, S.OUT_OF_SERVICE}),
    S.OUT_OF_SERVICE: frozenset({S.AVAILABLE}),
}

# A dine-in order may not be seated at a table staff have closed off.
UNSEATABLE_STATUSES = frozenset({S.CLEANING, S.OUT_OF_SERVICE})

ACTIVE_ORDER_STATUSES = (
    OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.SERVED,
)


class TableError(ValidationError):
    pass


def _validated(name: str | None, capacity) -> tuple[str, int]:
    errors: dict[str, list[str]] = {}
    name = (name or "").strip()
    if not name or len(name) > 40:
        errors["name"] = ["Table name is required (max 40 characters)."]
    try:
        capacity = int(capacity)
    except (TypeError, ValueError):
        capacity = None
    if capacity is None or not MIN_CAPACITY <= capacity <= MAX_CAPACITY:
        errors["capacity"] = [f"Capacity must be a whole number from {MIN_CAPACITY} to {MAX_CAPACITY}."]
    if errors:
        raise TableError(errors)
    return name, capacity


def _commit_or_duplicate(name: str) -> None:
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise TableError({"name": [f"A table named {name!r} already exists."]})


def create_table(name: str | None, capacity) -> RestaurantTable:
    name, capacity = _validated(name, capacity)
    table = RestaurantTable(name=name, capacity=capacity, status=TableStatus.AVAILABLE)
    db.session.add(table)
    _commit_or_duplicate(name)
    return table


def update_table(table: RestaurantTable, name: str | None, capacity) -> None:
    table.name, table.capacity = _validated(name, capacity)
    _commit_or_duplicate(table.name)


def change_table_status(table: RestaurantTable, raw_status: str | None) -> TableStatus:
    """Returns the previous status; raises ``TableError`` on an unknown value
    or a transition outside the allow-list."""
    try:
        new_status = TableStatus(raw_status)
    except ValueError:
        raise TableError({"status": ["Unknown table status."]})
    previous = table.status
    if new_status not in ALLOWED_TABLE_TRANSITIONS[previous]:
        raise TableError({"status": [f"Cannot change a table from {previous.value} to {new_status.value}."]})
    table.status = new_status
    db.session.commit()
    return previous


def release_table_if_idle(table_id: int) -> None:
    """OCCUPIED -> CLEANING once no active order remains on the table. Called
    inside the order transition's transaction (no commit here), so the table
    and the order change together."""
    table = db.session.get(RestaurantTable, table_id)
    still_busy = db.session.query(Order.id).filter(
        Order.table_id == table_id, Order.status.in_(ACTIVE_ORDER_STATUSES)
    ).first()
    if table.status == S.OCCUPIED and still_busy is None:
        table.status = S.CLEANING


def list_tables() -> list[RestaurantTable]:
    return db.session.query(RestaurantTable).order_by(RestaurantTable.name).all()


def seatable_tables() -> list[RestaurantTable]:
    return [t for t in list_tables() if t.status not in UNSEATABLE_STATUSES]


def active_orders_by_table() -> dict[int, Order]:
    """Newest unfinished order per table, items eager-loaded, for the board."""
    orders = (
        db.session.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.table_id.isnot(None), Order.status.in_(ACTIVE_ORDER_STATUSES))
        .order_by(Order.created_at, Order.id)
        .all()
    )
    return {order.table_id: order for order in orders}  # later rows overwrite: newest wins
