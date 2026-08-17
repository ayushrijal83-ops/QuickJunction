"""Public menu browsing. No login required, no write operations.

Every query here goes through app.services.menu's public functions, which
filter to active category + available item unconditionally -- there is no
code path in this module that can read a price, an id, or anything else
from the request and use it to bypass that filter or override a price.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template, request

from app.routes.forms import AddToCartForm
from app.services.menu import get_public_menu_item, list_public_categories, list_public_menu_items

menu_bp = Blueprint("menu", __name__, url_prefix="/menu")


@menu_bp.get("")
def index():
    category_id = request.args.get("category_id", type=int)
    items = list_public_menu_items(category_id=category_id)
    categories = list_public_categories()
    return render_template(
        "menu/index.html",
        items=items,
        categories=categories,
        selected_category_id=category_id,
        add_form=AddToCartForm(),
    )


@menu_bp.get("/<int:item_id>")
def detail(item_id: int):
    item = get_public_menu_item(item_id)
    if item is None:
        abort(404)
    return render_template("menu/detail.html", item=item, add_form=AddToCartForm())
