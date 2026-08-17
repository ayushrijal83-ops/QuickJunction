"""Admin/staff menu management.

Role split, enforced entirely server-side via ``require_role``: STAFF can
view (``/admin/menu``); everything that writes -- categories, menu items,
ingredients -- is ADMIN only. No route here reads a price, a role, or an
id-to-modify from anywhere but validated form data and the URL's own
``<int:...>`` converter (which SQLAlchemy then re-fetches by primary key --
never trusted as "this row belongs to this request").
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.category import Category
from app.models.menu_item import MenuItem
from app.models.user import Role
from app.routes.forms import CategoryForm, MenuItemForm
from app.services.audit import record_event
from app.services.menu import (
    CategoryError,
    CategoryInput,
    MenuItemError,
    MenuItemInput,
    create_category,
    create_menu_item,
    list_admin_categories,
    list_admin_menu_items,
    parse_ingredient_names,
    sync_menu_item_ingredients,
    update_category,
    update_menu_item,
)
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

admin_menu_bp = Blueprint("admin_menu", __name__, url_prefix="/admin")


def _apply_form_errors(form, errors: dict[str, list[str]]) -> None:
    for field_name, messages in errors.items():
        if hasattr(form, field_name):
            getattr(form, field_name).errors.extend(messages)
        else:
            for message in messages:
                flash(message, "error")


def _audit(event_type: AuditEvent, **metadata) -> None:
    record_event(
        event_type,
        success=True,
        user_id=get_current_user().id,
        ip_address=client_ip(),
        user_agent=user_agent(),
        metadata=metadata or None,
    )


def _category_choices() -> list[tuple[int, str]]:
    return [
        (c.id, c.name if c.is_active else f"{c.name} (inactive)") for c in list_admin_categories()
    ]


# --- categories --------------------------------------------------------


@admin_menu_bp.get("/categories")
@require_role(Role.ADMIN)
def category_list():
    return render_template("admin/category_list.html", categories=list_admin_categories())


@admin_menu_bp.route("/categories/create", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def category_create():
    form = CategoryForm()
    if form.validate_on_submit():
        try:
            category = create_category(
                CategoryInput(name=form.name.data, description=form.description.data, is_active=form.is_active.data)
            )
        except CategoryError as exc:
            _apply_form_errors(form, exc.errors)
        else:
            _audit(AuditEvent.CATEGORY_CREATED, category_id=category.id, name=category.name)
            flash(f'Category "{category.name}" created.', "success")
            return redirect(url_for("admin_menu.category_list"))

    return render_template("admin/category_form.html", form=form, category=None)


@admin_menu_bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def category_edit(category_id: int):
    category = db.session.get(Category, category_id)
    if category is None:
        abort(404)

    form = CategoryForm() if request.method == "POST" else CategoryForm(obj=category)
    if form.validate_on_submit():
        was_active = category.is_active
        try:
            update_category(
                category,
                CategoryInput(name=form.name.data, description=form.description.data, is_active=form.is_active.data),
            )
        except CategoryError as exc:
            _apply_form_errors(form, exc.errors)
        else:
            if was_active and not category.is_active:
                _audit(AuditEvent.CATEGORY_DEACTIVATED, category_id=category.id, name=category.name)
            else:
                _audit(AuditEvent.CATEGORY_UPDATED, category_id=category.id, name=category.name)
            flash(f'Category "{category.name}" updated.', "success")
            return redirect(url_for("admin_menu.category_list"))

    return render_template("admin/category_form.html", form=form, category=category)


# --- menu items ------------------------------------------------------------


@admin_menu_bp.get("/menu")
@require_role(Role.STAFF, Role.ADMIN)
def menu_list():
    return render_template("admin/menu_list.html", items=list_admin_menu_items())


@admin_menu_bp.route("/menu/create", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def menu_create():
    form = MenuItemForm()
    form.category_id.choices = _category_choices()

    if form.validate_on_submit():
        try:
            item = create_menu_item(
                MenuItemInput(
                    category_id=form.category_id.data,
                    name=form.name.data,
                    description=form.description.data,
                    price=form.price.data,
                    cuisine=form.cuisine.data,
                    spice_level=form.spice_level.data,
                    dietary_type=form.dietary_type.data,
                    is_available=form.is_available.data,
                )
            )
        except MenuItemError as exc:
            _apply_form_errors(form, exc.errors)
        else:
            _audit(AuditEvent.MENU_ITEM_CREATED, menu_item_id=item.id, name=item.name)
            ingredient_names = parse_ingredient_names(form.ingredients.data)
            if ingredient_names and sync_menu_item_ingredients(item, ingredient_names):
                _audit(AuditEvent.INGREDIENT_CHANGED, menu_item_id=item.id)
            flash(f'Menu item "{item.name}" created.', "success")
            return redirect(url_for("admin_menu.menu_list"))

    return render_template("admin/menu_form.html", form=form, item=None)


@admin_menu_bp.route("/menu/<int:item_id>/edit", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def menu_edit(item_id: int):
    item = db.session.get(MenuItem, item_id)
    if item is None:
        abort(404)

    if request.method == "POST":
        form = MenuItemForm()
    else:
        form = MenuItemForm(obj=item)
        form.ingredients.data = ", ".join(ing.name for ing in item.ingredients)
    form.category_id.choices = _category_choices()

    if form.validate_on_submit():
        was_available = item.is_available
        try:
            update_menu_item(
                item,
                MenuItemInput(
                    category_id=form.category_id.data,
                    name=form.name.data,
                    description=form.description.data,
                    price=form.price.data,
                    cuisine=form.cuisine.data,
                    spice_level=form.spice_level.data,
                    dietary_type=form.dietary_type.data,
                    is_available=form.is_available.data,
                ),
            )
        except MenuItemError as exc:
            _apply_form_errors(form, exc.errors)
        else:
            if was_available != item.is_available:
                _audit(AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED, menu_item_id=item.id, is_available=item.is_available)
            else:
                _audit(AuditEvent.MENU_ITEM_UPDATED, menu_item_id=item.id, name=item.name)

            ingredient_names = parse_ingredient_names(form.ingredients.data)
            if sync_menu_item_ingredients(item, ingredient_names):
                _audit(AuditEvent.INGREDIENT_CHANGED, menu_item_id=item.id)

            flash(f'Menu item "{item.name}" updated.', "success")
            return redirect(url_for("admin_menu.menu_list"))

    return render_template("admin/menu_form.html", form=form, item=item)
