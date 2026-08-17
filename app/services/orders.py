"""Checkout, order history, and staff order management.

Checkout is the one place in the codebase that turns a session cart into a
persisted, money-bearing record. The cart (app/utils/cart.py) never held
anything but a menu item id and a quantity; every price used below is
re-read from ``MenuItem`` inside this module's own transaction. Nothing
supplied by a client -- a unit price, a line total, a subtotal, a total --
is ever accepted as authoritative here or anywhere upstream of it.

The staff-facing half (bottom of this module) never touches money at all:
a status change moves one column and must never recalculate a historical
price from the current menu -- the ``OrderItem`` snapshots written at
checkout are the only prices an order ever has.

The whole validate-then-create sequence runs as one transaction: either a
complete, correctly-priced ``Order`` + its ``OrderItem`` rows commit
together, or nothing is written at all (``checkout`` rolls back and raises
``CheckoutError`` on any failure -- see the try/except around the write
below). No flask.request or flask.session here; callers (app/routes/orders.py)
own the HTTP layer and the session cart.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.order import Order, OrderItem, OrderStatus
from app.services.cart import MAX_QUANTITY_PER_ITEM, MIN_QUANTITY, get_available_menu_item
from app.services.errors import ValidationError

MAX_DISTINCT_LINES = 30


class CheckoutError(ValidationError):
    pass


@dataclass
class _Line:
    menu_item_id: int
    quantity: int


def _parse_cart(cart: dict[str, int]) -> list[_Line]:
    lines: list[_Line] = []
    for key, quantity in cart.items():
        try:
            menu_item_id = int(key)
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise CheckoutError({"cart": ["Your cart contains an invalid item and cannot be checked out."]})
        lines.append(_Line(menu_item_id, quantity))
    return lines


def checkout(user_id: int, cart: dict[str, int]) -> Order:
    """Validate ``cart`` against the live menu and create an ``Order`` with
    its ``OrderItem`` rows in a single transaction.

    Steps (all server-side, none trusting the caller): parse the cart,
    reload each menu item from the database, verify it is still available,
    verify each quantity is in bounds, recompute every unit price and line
    total, sum the subtotal/total, then write the order. Any failure raises
    ``CheckoutError`` and leaves no partial order behind.
    """
    if not cart:
        raise CheckoutError({"cart": ["Your cart is empty."]})

    lines = _parse_cart(cart)
    if len(lines) > MAX_DISTINCT_LINES:
        raise CheckoutError({"cart": ["Your cart has too many distinct items."]})

    problems: list[str] = []
    resolved: list[tuple[_Line, "MenuItem"]] = []  # noqa: F821
    for line in lines:
        if line.quantity < MIN_QUANTITY or line.quantity > MAX_QUANTITY_PER_ITEM:
            problems.append(f"Invalid quantity for item {line.menu_item_id}.")
            continue
        item = get_available_menu_item(line.menu_item_id)
        if item is None:
            problems.append(f"Item {line.menu_item_id} is no longer available.")
            continue
        resolved.append((line, item))

    if problems:
        raise CheckoutError({"cart": problems})

    order = Order(user_id=user_id, status=OrderStatus.PENDING, subtotal=Decimal("0.00"), total=Decimal("0.00"))
    subtotal = Decimal("0.00")

    try:
        db.session.add(order)
        db.session.flush()  # assigns order.id for the OrderItem FK below

        for line, item in resolved:
            line_total = (item.price * line.quantity).quantize(Decimal("0.01"))
            subtotal += line_total
            db.session.add(
                OrderItem(
                    order_id=order.id,
                    menu_item_id=item.id,
                    item_name_snapshot=item.name,
                    unit_price_snapshot=item.price,
                    quantity=line.quantity,
                    line_total=line_total,
                )
            )

        order.subtotal = subtotal
        order.total = subtotal  # no tax, discount, or delivery fee in this milestone
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise CheckoutError({"cart": ["Could not place your order. Please try again."]})

    return order


def list_orders_for_user(user_id: int) -> list[Order]:
    return db.session.query(Order).filter_by(user_id=user_id).order_by(Order.created_at.desc()).all()


def get_order_for_user(order_id: int, user_id: int) -> Order | None:
    """Ownership is enforced in the query itself, not checked after the
    fact -- the IDOR-safe way to answer "does this order belong to this
    user": a mismatched id and a non-existent id both return ``None``."""
    return db.session.query(Order).filter_by(id=order_id, user_id=user_id).first()


# --- staff order management -------------------------------------------------

# The kitchen workflow, as an explicit allow-list. Anything not named here
# is rejected -- including same-status "changes" and every backwards step
# (COMPLETED -> PREPARING, PENDING -> COMPLETED, ...). Both terminal states
# map to an empty set, so an order that is finished or cancelled can never
# move again.
ALLOWED_STATUS_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.CONFIRMED, OrderStatus.CANCELLED}),
    OrderStatus.CONFIRMED: frozenset({OrderStatus.PREPARING, OrderStatus.CANCELLED}),
    OrderStatus.PREPARING: frozenset({OrderStatus.READY, OrderStatus.CANCELLED}),
    OrderStatus.READY: frozenset({OrderStatus.COMPLETED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


class OrderStatusError(ValidationError):
    pass


def is_valid_transition(current: OrderStatus, new: OrderStatus) -> bool:
    return new in ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())


def allowed_next_statuses(current: OrderStatus) -> list[OrderStatus]:
    """Drives the staff UI's status buttons. The template rendering these is
    a convenience only -- ``update_order_status`` re-checks the transition
    server-side regardless of what was rendered or posted."""
    return sorted(ALLOWED_STATUS_TRANSITIONS.get(current, frozenset()), key=lambda s: s.value)


def list_orders_for_staff() -> list[Order]:
    """Every order, newest first, with items eagerly loaded so the queue's
    per-order item count does not fire one query per row."""
    return (
        db.session.query(Order)
        .options(selectinload(Order.items), selectinload(Order.customer))
        .order_by(Order.created_at.desc(), Order.id.desc())
        .all()
    )


def get_order_for_staff(order_id: int) -> Order | None:
    """Staff are authorized against *any* order (that is the job), so there
    is no ownership filter here -- unlike ``get_order_for_user``. The role
    check lives in the route decorator, never in a client-supplied value."""
    return db.session.get(Order, order_id)


def update_order_status(order: Order, new_status: OrderStatus) -> OrderStatus:
    """Move ``order`` to ``new_status`` if the workflow permits it.

    Returns the previous status (the caller needs it for the audit record).
    Raises ``OrderStatusError`` on any disallowed transition, leaving the
    order untouched. Nothing about the order's money is recalculated here:
    a status change must never rewrite a stored price snapshot.
    """
    previous = order.status
    if not is_valid_transition(previous, new_status):
        raise OrderStatusError(
            {"status": [f"Cannot change an order from {previous.value} to {new_status.value}."]}
        )

    order.status = new_status
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise OrderStatusError({"status": ["Could not update the order. Please try again."]})
    return previous


def coerce_status(raw: str) -> OrderStatus:
    """Parse a client-supplied status string against the enum's allow-list.
    A value outside the vocabulary never reaches the transition check."""
    try:
        return OrderStatus(raw)
    except ValueError:
        raise OrderStatusError({"status": ["Unknown order status."]})
