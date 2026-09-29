"""Inventory (M13): stock list, movement history, manual movements, ingredient
definitions and recipes.

STAFF and ADMIN may view stock and record purchases, restocks and waste;
ADJUSTMENT (a stock-count correction), ingredient definitions and recipes are
ADMIN only. Roles come from ``require_role`` (and the service re-checks
ADJUSTMENT); every POST is CSRF-protected app-wide and audited.
"""

from __future__ import annotations

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.ingredient import Ingredient, StockUnit
from app.models.menu_item import MenuItem
from app.models.stock_movement import MovementType
from app.models.user import Role
from app.routes.forms import IngredientForm, RecipeForm, StockMovementForm
from app.services.audit import record_event
from app.services.inventory import (
    MANUAL_TYPES,
    InventoryError,
    add_to_recipe,
    history,
    list_ingredients,
    movement_totals,
    recipe_lines,
    record_movement,
    save_ingredient,
    set_recipe,
    since_days,
)
from app.utils.authorization import get_current_user, require_role
from app.utils.clock import db_today
from app.utils.request_meta import client_ip, user_agent

inventory_bp = Blueprint("inventory", __name__)

WINDOWS = (7, 30, 90)
_QTY_FIELD = re.compile(r"^q_(\d+)$")


def _audit(event: AuditEvent, metadata: dict) -> None:
    record_event(event, success=True, user_id=get_current_user().id, ip_address=client_ip(),
                 user_agent=user_agent(), metadata=metadata)


def _flash_errors(exc: InventoryError) -> None:
    for messages in exc.errors.values():
        for message in messages:
            flash(message, "error")


def _ingredient(ingredient_id: int) -> Ingredient:
    ingredient = db.session.get(Ingredient, ingredient_id)
    if ingredient is None:
        abort(404)
    return ingredient


@inventory_bp.get("/staff/inventory")
@require_role(Role.STAFF, Role.ADMIN)
def stock():
    days = request.args.get("days", 30, type=int)
    days = days if days in WINDOWS else 30
    ingredients = list_ingredients()
    return render_template(
        "inventory/list.html",
        ingredients=ingredients,
        low=[i for i in ingredients if i.is_low],
        totals=movement_totals(since_days(days, db_today())),
        days=days,
        windows=WINDOWS,
        MovementType=MovementType,
    )


@inventory_bp.get("/staff/inventory/<int:ingredient_id>")
@require_role(Role.STAFF, Role.ADMIN)
def detail(ingredient_id: int):
    ingredient = _ingredient(ingredient_id)
    allowed = [t for t in MANUAL_TYPES if t != MovementType.ADJUSTMENT or get_current_user().role == Role.ADMIN]
    return render_template("inventory/detail.html", ingredient=ingredient, movements=history(ingredient),
                           form=StockMovementForm(), movement_types=allowed)


@inventory_bp.post("/staff/inventory/<int:ingredient_id>/movement")
@require_role(Role.STAFF, Role.ADMIN)
def movement(ingredient_id: int):
    ingredient = _ingredient(ingredient_id)
    form = StockMovementForm()
    if not form.validate_on_submit():
        flash("Enter a movement type and quantity.", "error")
        return redirect(url_for("inventory.detail", ingredient_id=ingredient.id))
    try:
        moved = record_movement(ingredient, form.movement_type.data, form.quantity.data, form.note.data,
                                get_current_user())
    except InventoryError as exc:
        _flash_errors(exc)
        return redirect(url_for("inventory.detail", ingredient_id=ingredient.id))
    _audit(AuditEvent.STOCK_MOVEMENT_RECORDED, {
        "ingredient_id": ingredient.id, "movement_id": moved.id, "type": moved.movement_type.value,
        "change": str(moved.quantity_change), "after": str(moved.quantity_after)})
    flash(f"{ingredient.name}: {moved.movement_type.value} recorded; now {moved.quantity_after} {ingredient.unit.value}.",
          "success")
    return redirect(url_for("inventory.detail", ingredient_id=ingredient.id))


def _save(ingredient: Ingredient | None):
    form = IngredientForm(obj=ingredient)
    if ingredient is not None and not form.is_submitted():
        form.unit.data = ingredient.unit.value
    if form.validate_on_submit():
        try:
            saved = save_ingredient(ingredient, form.name.data, form.unit.data, form.minimum_quantity.data,
                                    form.is_active.data)
        except InventoryError as exc:
            if ingredient is not None:
                db.session.refresh(ingredient)
            _flash_errors(exc)
        else:
            _audit(AuditEvent.STOCK_ITEM_SAVED, {"ingredient_id": saved.id, "created": ingredient is None})
            flash(f"{saved.name} saved.", "success")
            return redirect(url_for("inventory.detail", ingredient_id=saved.id))
    return render_template("inventory/form.html", form=form, ingredient=ingredient, units=list(StockUnit))


@inventory_bp.route("/admin/inventory/new", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def new():
    return _save(None)


@inventory_bp.route("/admin/inventory/<int:ingredient_id>/edit", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def edit(ingredient_id: int):
    return _save(_ingredient(ingredient_id))


@inventory_bp.route("/admin/menu/<int:item_id>/recipe", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def recipe(item_id: int):
    item = db.session.get(MenuItem, item_id)
    if item is None:
        abort(404)
    form = RecipeForm()
    if form.validate_on_submit():
        quantities = {int(m.group(1)): value for key, value in request.form.items()
                      if (m := _QTY_FIELD.match(key))}
        try:
            set_recipe(item, quantities)
            add_id, add_qty = request.form.get("add_ingredient_id", ""), request.form.get("add_quantity", "")
            if add_id:
                add_to_recipe(item, _ingredient(int(add_id)) if add_id.isdigit() else abort(404), add_qty)
        except InventoryError as exc:
            db.session.rollback()
            _flash_errors(exc)
        else:
            _audit(AuditEvent.RECIPE_UPDATED, {"menu_item_id": item.id})
            flash(f"Recipe for {item.name} saved.", "success")
            return redirect(url_for("inventory.recipe", item_id=item.id))
    lines = recipe_lines(item)
    linked = {line.ingredient_id for line in lines}
    return render_template("inventory/recipe.html", item=item, lines=lines, form=form,
                           addable=[i for i in list_ingredients() if i.id not in linked and i.is_active])
