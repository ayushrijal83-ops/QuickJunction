"""Cart business logic: quantity validation and resolving a session cart
against the live menu.

The session (app/utils/cart.py) stores only ``{menu_item_id: quantity}`` --
never a price. Every function here re-fetches ``MenuItem`` from the
database through the same "active category + available item" filter as the
public menu (app/services/menu.py), so a menu item deactivated or repriced
after being added to a cart is caught on the very next read, not just at
checkout. No function here reads ``flask.request`` or ``flask.session`` --
callers (app/routes/cart.py) own the session and pass/receive plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.extensions import db
from app.models.category import Category
from app.models.menu_item import MenuItem
from app.services.errors import ValidationError

MIN_QUANTITY = 1
MAX_QUANTITY_PER_ITEM = 20
MAX_DISTINCT_ITEMS = 30


class CartError(ValidationError):
    pass


def validate_quantity(quantity: int) -> list[str]:
    errors = []
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        return ["Quantity must be a whole number."]
    if quantity < MIN_QUANTITY:
        errors.append(f"Quantity must be at least {MIN_QUANTITY}.")
    if quantity > MAX_QUANTITY_PER_ITEM:
        errors.append(f"Quantity must be at most {MAX_QUANTITY_PER_ITEM} per item.")
    return errors


def get_available_menu_item(menu_item_id: int) -> MenuItem | None:
    """Same filter as app/services/menu.py::get_public_menu_item -- a cart
    (or an order) can never hold an item a customer could not have found by
    browsing the public menu."""
    return (
        db.session.query(MenuItem)
        .join(Category)
        .filter(MenuItem.id == menu_item_id, MenuItem.is_available.is_(True), Category.is_active.is_(True))
        .first()
    )


def add_item(cart: dict[str, int], menu_item_id: int, quantity: int) -> dict[str, int]:
    errors: dict[str, list[str]] = {}

    if quantity_errors := validate_quantity(quantity):
        errors["quantity"] = quantity_errors

    item = get_available_menu_item(menu_item_id) if isinstance(menu_item_id, int) else None
    if item is None:
        errors.setdefault("menu_item_id", []).append("This menu item is not available.")

    if errors:
        raise CartError(errors)

    key = str(menu_item_id)
    new_cart = dict(cart)
    updated_quantity = new_cart.get(key, 0) + quantity

    if updated_quantity > MAX_QUANTITY_PER_ITEM:
        raise CartError({"quantity": [f"Quantity must be at most {MAX_QUANTITY_PER_ITEM} per item."]})
    if key not in new_cart and len(new_cart) >= MAX_DISTINCT_ITEMS:
        raise CartError({"menu_item_id": [f"Cart cannot hold more than {MAX_DISTINCT_ITEMS} different items."]})

    new_cart[key] = updated_quantity
    return new_cart


def update_item(cart: dict[str, int], menu_item_id: int, quantity: int) -> dict[str, int]:
    key = str(menu_item_id)
    if key not in cart:
        raise CartError({"menu_item_id": ["That item is not in your cart."]})

    if quantity_errors := validate_quantity(quantity):
        raise CartError({"quantity": quantity_errors})

    new_cart = dict(cart)
    new_cart[key] = quantity
    return new_cart


def remove_item(cart: dict[str, int], menu_item_id: int) -> dict[str, int]:
    new_cart = dict(cart)
    new_cart.pop(str(menu_item_id), None)
    return new_cart


@dataclass
class CartLine:
    menu_item_id: int
    quantity: int
    menu_item: MenuItem | None
    unit_price: Decimal | None
    line_total: Decimal | None
    available: bool


@dataclass
class CartView:
    lines: list[CartLine]
    subtotal: Decimal
    item_count: int
    has_unavailable: bool


def build_cart_view(cart: dict[str, int]) -> CartView:
    """Read-only projection of the session cart against the live menu.
    Never trusts a stored quantity's type -- a corrupted or hand-edited
    session value is dropped rather than crashing the cart page."""
    lines: list[CartLine] = []
    subtotal = Decimal("0.00")
    has_unavailable = False

    for key, quantity in cart.items():
        try:
            menu_item_id = int(key)
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue

        item = get_available_menu_item(menu_item_id)
        if item is None:
            lines.append(CartLine(menu_item_id, quantity, None, None, None, False))
            has_unavailable = True
            continue

        line_total = (item.price * quantity).quantize(Decimal("0.01"))
        subtotal += line_total
        lines.append(CartLine(menu_item_id, quantity, item, item.price, line_total, True))

    item_count = sum(line.quantity for line in lines)
    return CartView(lines=lines, subtotal=subtotal, item_count=item_count, has_unavailable=has_unavailable)
