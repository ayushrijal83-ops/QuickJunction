"""Session cart: GET /cart, POST /cart/add|update|remove|clear.

Every mutating route here is CSRF-protected the same way as every other
state-changing form in this app (a FlaskForm carrying a csrf_token field,
checked by Flask-WTF's CSRFProtect before the view body runs). No route in
this module reads a price from the request -- only a menu item id and a
quantity, both re-validated against the database by app/services/cart.py
before anything is written to the session. Anonymous visitors may hold a
cart; only checkout (app/routes/orders.py) requires login.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, url_for

from app.routes.forms import AddToCartForm, ClearCartForm, RemoveFromCartForm, UpdateCartForm
from app.services.cart import CartError, add_item, build_cart_view, remove_item, update_item
from app.utils.cart import clear_cart, get_cart, save_cart

cart_bp = Blueprint("cart", __name__, url_prefix="/cart")


def _flash_errors(exc: CartError) -> None:
    for messages in exc.errors.values():
        for message in messages:
            flash(message, "error")


@cart_bp.get("")
def index():
    cart_view = build_cart_view(get_cart())
    return render_template(
        "cart/index.html",
        cart=cart_view,
        add_form=AddToCartForm(),
        update_form=UpdateCartForm(),
        remove_form=RemoveFromCartForm(),
        clear_form=ClearCartForm(),
    )


@cart_bp.post("/add")
def add():
    form = AddToCartForm()
    if form.validate_on_submit():
        try:
            save_cart(add_item(get_cart(), form.menu_item_id.data, form.quantity.data))
        except CartError as exc:
            _flash_errors(exc)
        else:
            flash("Added to cart.", "success")
    else:
        flash("Could not add that item to your cart.", "error")
    return redirect(url_for("cart.index"))


@cart_bp.post("/update")
def update():
    form = UpdateCartForm()
    if form.validate_on_submit():
        try:
            save_cart(update_item(get_cart(), form.menu_item_id.data, form.quantity.data))
        except CartError as exc:
            _flash_errors(exc)
        else:
            flash("Cart updated.", "success")
    else:
        flash("Could not update your cart.", "error")
    return redirect(url_for("cart.index"))


@cart_bp.post("/remove")
def remove():
    form = RemoveFromCartForm()
    if form.validate_on_submit():
        save_cart(remove_item(get_cart(), form.menu_item_id.data))
        flash("Item removed.", "success")
    else:
        flash("Could not remove that item.", "error")
    return redirect(url_for("cart.index"))


@cart_bp.post("/clear")
def clear():
    form = ClearCartForm()
    if form.validate_on_submit():
        clear_cart()
        flash("Cart cleared.", "success")
    else:
        flash("Could not clear your cart.", "error")
    return redirect(url_for("cart.index"))
