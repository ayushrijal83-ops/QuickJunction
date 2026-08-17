"""Session-backed cart storage.

The cart is a plain ``{"<menu_item_id>": quantity}`` dict inside Flask's
signed session cookie -- never a separate table. Only a menu item id and a
quantity are ever stored, never a price, a name, or a total. Flask signs
the session with ``SECRET_KEY`` (itsdangerous), so a client can inspect but
cannot alter its contents without invalidating the signature; a tampered
cookie is rejected by Flask before a request ever reaches a view. That is
what makes a session cart "a secure mechanism" for this milestone without a
new database table or an extra dependency.

Nothing here treats a stored id or quantity as final: app/services/cart.py
re-resolves every line against the current MenuItem table on every read,
and checkout re-validates everything again inside its own transaction
(app/services/orders.py).
"""

from __future__ import annotations

from flask import session

_SESSION_KEY = "cart"


def get_cart() -> dict[str, int]:
    return dict(session.get(_SESSION_KEY) or {})


def save_cart(cart: dict[str, int]) -> None:
    if cart:
        session[_SESSION_KEY] = cart
    else:
        session.pop(_SESSION_KEY, None)
    session.modified = True


def clear_cart() -> None:
    session.pop(_SESSION_KEY, None)
    session.modified = True
