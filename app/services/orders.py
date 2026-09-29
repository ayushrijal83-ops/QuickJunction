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

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.order import CancellationActor, Order, OrderItem, OrderSource, OrderStatus
from app.models.payment import Payment
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role, User
from app.services.cart import MAX_QUANTITY_PER_ITEM, MIN_QUANTITY, get_available_menu_item
from app.services.errors import ValidationError
from app.services.inventory import deduct_for_order, shortages
from app.services.pricing import current_settings, price
from app.services.tables import UNSEATABLE_STATUSES, release_table_if_idle

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


def resolve_order_channel(raw_source, raw_table_id) -> tuple[OrderSource, int | None]:
    """Validate a client-supplied (source, table) pair.

    DINE_IN requires an existing, seatable table; every other source must not
    carry one. Both are re-checked by ``ck_orders_source_table`` in the
    database, so a bypass of this function still cannot store a mismatch.
    """
    try:
        source = OrderSource(raw_source or OrderSource.ONLINE.value)
    except ValueError:
        raise CheckoutError({"source": ["Unknown order type."]})

    table_id = None
    if raw_table_id not in (None, ""):
        try:
            table_id = int(raw_table_id)
        except (TypeError, ValueError):
            raise CheckoutError({"table_id": ["Unknown table."]})

    if source != OrderSource.DINE_IN:
        if table_id is not None:
            raise CheckoutError({"table_id": ["Only dine-in orders can be assigned a table."]})
        return source, None

    if table_id is None:
        raise CheckoutError({"table_id": ["Choose your table for a dine-in order."]})
    table = db.session.get(RestaurantTable, table_id)
    if table is None:
        raise CheckoutError({"table_id": ["Unknown table."]})
    if table.status in UNSEATABLE_STATUSES:
        raise CheckoutError({"table_id": [f"Table {table.name} is not available right now."]})
    return source, table.id


def checkout(
    user_id: int,
    cart: dict[str, int],
    source: OrderSource | str = OrderSource.ONLINE,
    table_id: int | None = None,
) -> Order:
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

    short = shortages({line.menu_item_id: line.quantity for line, _ in resolved})
    if short:
        raise CheckoutError({"cart": [f"Sorry, we are out of {', '.join(short)} for this order right now."]})

    # Re-validated here, not only in the route: the channel rule is a
    # business rule, and callers other than the checkout route exist.
    source, table_id = resolve_order_channel(source, table_id)
    # Read before the order is built: a query here would autoflush a
    # half-priced order row and trip ck_orders_total_formula.
    tax_rate = current_settings()[0]

    order = Order(
        user_id=user_id,
        status=OrderStatus.PENDING,
        source=source,
        table_id=table_id,
        subtotal=Decimal("0.00"),
        total=Decimal("0.00"),
    )
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

        # Tax at today's configured rate, snapshotted on the order (M12);
        # checkout never discounts -- only staff can, later, on an unpaid order.
        tax_amount, total = price(subtotal, Decimal("0.00"), tax_rate)
        order.subtotal, order.tax_rate, order.tax_amount, order.total = subtotal, tax_rate, tax_amount, total
        if table_id is not None:
            # A party ordering at a table means it is in use. Freeing it again
            # (CLEANING -> AVAILABLE) is a staff action on the table board.
            db.session.get(RestaurantTable, table_id).status = TableStatus.OCCUPIED
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
    # SERVED is the dine-in step; takeaway/delivery go READY -> COMPLETED.
    OrderStatus.READY: frozenset({OrderStatus.SERVED, OrderStatus.COMPLETED}),
    OrderStatus.SERVED: frozenset({OrderStatus.COMPLETED}),
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
        .options(selectinload(Order.items), selectinload(Order.customer), selectinload(Order.table),
                 selectinload(Order.payment))
        .order_by(Order.created_at.desc(), Order.id.desc())
        .all()
    )


def get_order_for_staff(order_id: int) -> Order | None:
    """Staff are authorized against *any* order (that is the job), so there
    is no ownership filter here -- unlike ``get_order_for_user``. The role
    check lives in the route decorator, never in a client-supplied value."""
    return db.session.get(Order, order_id)


def _actor_for(user: User) -> CancellationActor:
    return {
        Role.CUSTOMER: CancellationActor.CUSTOMER,
        Role.STAFF: CancellationActor.STAFF,
        Role.ADMIN: CancellationActor.ADMIN,
    }[user.role]


def _apply_transition(order: Order, from_statuses: frozenset[OrderStatus], new_status: OrderStatus,
                      *, actor: User | None, reason: str | None, owner_id: int | None = None) -> bool:
    """Move ``order`` to ``new_status`` **only if its current database row is
    still in ``from_statuses``**, in one conditional UPDATE.

    This is the concurrency guard. A read-then-write would let a customer's
    cancel and a cook's "start preparing" both pass their checks against the
    same stale PENDING read, and the later write would win. Here the status
    check happens inside the UPDATE itself, so the database (a row lock on
    InnoDB) serialises the two: exactly one matches, the other updates zero
    rows. Returns whether this caller won; ``order`` is refreshed either way.

    Cancelling also requires that no unrefunded payment exists -- checked in
    the same UPDATE, against the same order row ``record_payment`` locks, so
    a paid order can never be cancelled while still holding the money.
    Finishing a dine-in order (completed/cancelled) releases its table to
    CLEANING in the same transaction when nothing else is active there.
    """
    values: dict = {"status": new_status}
    if new_status == OrderStatus.PREPARING:
        values["preparing_at"] = sa.func.now()
    elif new_status == OrderStatus.READY:
        values["ready_at"] = sa.func.now()
    if new_status == OrderStatus.CANCELLED:
        values.update(
            cancelled_at=sa.func.now(),
            cancelled_by_id=actor.id if actor else None,
            cancellation_actor=_actor_for(actor) if actor else None,
            cancellation_reason=(reason or "").strip()[:255] or None,
        )
    stmt = (
        sa.update(Order)
        .where(Order.id == order.id, Order.status.in_(from_statuses))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if owner_id is not None:
        stmt = stmt.where(Order.user_id == owner_id)
    if new_status == OrderStatus.CANCELLED:
        stmt = stmt.where(~sa.exists().where(Payment.order_id == Order.id, Payment.refunded_amount < Payment.amount))
    try:
        won = db.session.execute(stmt).rowcount == 1
        if won and order.table_id is not None and new_status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
            release_table_if_idle(order.table_id)
        if won and new_status == OrderStatus.COMPLETED:
            # The sale's stock deduction commits with the completion or not at
            # all; idempotent (M13, app/services/inventory.py).
            deduct_for_order(order.id, actor.id if actor else None)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise OrderStatusError({"status": ["Could not update the order. Please try again."]})
    db.session.refresh(order)
    return won


def holds_payment(order: Order) -> bool:
    """True while money received for ``order`` has not been fully refunded."""
    return order.payment is not None and not order.payment.fully_refunded


def update_order_status(order: Order, new_status: OrderStatus, *, actor: User | None = None,
                        reason: str | None = None) -> OrderStatus:
    """Move ``order`` to ``new_status`` if the workflow permits it.

    Returns the previous status (the caller needs it for the audit record).
    Raises ``OrderStatusError`` on any disallowed transition -- including one
    that *was* allowed from the status the caller loaded but no longer is,
    because someone else changed the order in between. Nothing about the
    order's money is recalculated here: a status change must never rewrite a
    stored price snapshot.
    """
    previous = order.status
    if not is_valid_transition(previous, new_status):
        raise OrderStatusError(
            {"status": [f"Cannot change an order from {previous.value} to {new_status.value}."]}
        )
    if not _apply_transition(order, frozenset({previous}), new_status, actor=actor, reason=reason):
        if new_status == OrderStatus.CANCELLED and holds_payment(order):
            raise OrderStatusError(
                {"status": [f"Order #{order.id} has been paid. Refund the payment in full before cancelling."]}
            )
        raise OrderStatusError(
            {"status": [f"Order #{order.id} was changed by someone else (now {order.status.value}). "
                        "Nothing was updated."]}
        )
    return previous


# --- customer self-service cancellation --------------------------------------

# The customer rule: cancellable only until the kitchen starts. Deliberately
# narrower than staff's (who may also cancel PREPARING); both are subsets of
# ALLOWED_STATUS_TRANSITIONS, so neither path can create a new transition.
CUSTOMER_CANCELLABLE_STATUSES = frozenset({OrderStatus.PENDING, OrderStatus.CONFIRMED})


def customer_can_cancel(order: Order) -> bool:
    return order.status in CUSTOMER_CANCELLABLE_STATUSES and not holds_payment(order)


def cancel_order_as_customer(order: Order, customer: User, reason: str | None = None) -> OrderStatus:
    """Cancel ``customer``'s own ``order``. Returns the previous status.

    Ownership is enforced twice: callers load the order with
    ``get_order_for_user`` (404 otherwise), and the UPDATE itself is scoped to
    ``user_id``. The status rule is enforced inside that same UPDATE, so a
    cook starting preparation a moment earlier wins and this raises.
    """
    previous = order.status
    if order.user_id != customer.id:
        raise OrderStatusError({"status": ["Order not found."]})
    if previous == OrderStatus.CANCELLED:
        raise OrderStatusError({"status": ["This order is already cancelled."]})
    if previous not in CUSTOMER_CANCELLABLE_STATUSES or not _apply_transition(
        order, CUSTOMER_CANCELLABLE_STATUSES, OrderStatus.CANCELLED,
        actor=customer, reason=reason, owner_id=customer.id,
    ):
        if holds_payment(order):
            raise OrderStatusError(
                {"status": ["This order has already been paid, so it cannot be cancelled online. "
                            "Please speak to our staff."]}
            )
        raise OrderStatusError(
            {"status": [f"This order is already {order.status.value} and can no longer be cancelled. "
                        "Please speak to our staff."]}
        )
    return previous


def coerce_status(raw: str) -> OrderStatus:
    """Parse a client-supplied status string against the enum's allow-list.
    A value outside the vocabulary never reaches the transition check."""
    try:
        return OrderStatus(raw)
    except ValueError:
        raise OrderStatusError({"status": ["Unknown order status."]})
