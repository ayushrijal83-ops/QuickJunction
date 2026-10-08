"""Menu availability for the floor (M5): STAFF and ADMIN mark dishes sold
out or available again, without the admin-only edit form.

``POST /staff/menu/<id>/availability`` with ``available=1|0`` (CSRF:
app-wide CSRFProtect). The item comes from the URL and the only other input
is the target state, so this route can change ``is_available`` and nothing
else -- price, name, category, translations and recipe stay admin-only
(app/routes/admin_menu.py). The change and its
``menu_item_availability_changed`` audit row commit together.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.menu_item import MenuItem
from app.models.user import Role
from app.services.audit import record_event
from app.services.menu import list_admin_menu_items, set_availability
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

staff_menu_bp = Blueprint("staff_menu", __name__, url_prefix="/staff/menu")

_STATES = {"1": True, "0": False}


@staff_menu_bp.get("")
@require_role(Role.STAFF, Role.ADMIN)
def availability_list():
    return render_template("staff/menu.html", items=list_admin_menu_items())


@staff_menu_bp.post("/<int:item_id>/availability")
@require_role(Role.STAFF, Role.ADMIN)
def set_item_availability(item_id: int):
    available = _STATES.get(request.form.get("available", ""))
    if available is None:
        abort(400)
    item = db.session.get(MenuItem, item_id)
    if item is None:
        abort(404)
    name, actor = item.name, get_current_user()
    changed = set_availability(actor, item_id, available, audit=lambda i, a: record_event(
        AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED, success=True, user_id=actor.id, ip_address=client_ip(),
        user_agent=user_agent(), metadata={"menu_item_id": i, "is_available": a, "source": "staff_menu"}))
    if changed:
        flash(f'"{name}" is now {"available" if available else "sold out"}.', "success")
    else:
        flash(f'"{name}" was already {"available" if available else "sold out"}.', "success")
    return redirect(url_for("staff_menu.availability_list"))
