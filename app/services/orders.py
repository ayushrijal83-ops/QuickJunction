"""Checkout and order history.

Checkout is the one place in the codebase that turns a session cart into a
persisted, money-bearing record. The cart (app/utils/cart.py) never held
anything but a menu item id and a quantity; every price used below is
re-read from ``MenuItem`` inside this module's own transaction. Nothing
supplied by a client -- a unit price, a line total, a subtotal, a total --
is ever accepted as authoritative here or anywhere upstream of it.

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
