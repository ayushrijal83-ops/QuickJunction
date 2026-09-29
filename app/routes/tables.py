"""Restaurant tables: the staff floor board and admin table management.

STAFF and ADMIN may view the board and move a table through its status
allow-list (seating, cleaning, ...). Only ADMIN may create or rename tables or
change their capacity. Every role check is ``require_role`` on the server;
state changes are POST + CSRF (app-wide ``CSRFProtect``) and audited.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, url_for

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role
from app.routes.forms import TableForm, TableStatusForm
from app.services.audit import record_event
from app.services.tables import (
    ALLOWED_TABLE_TRANSITIONS,
    TableError,
    active_orders_by_table,
    change_table_status,
    create_table,
    list_tables,
    update_table,
)
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

tables_bp = Blueprint("tables", __name__)


def _audit(event: AuditEvent, metadata: dict) -> None:
    record_event(event, success=True, user_id=get_current_user().id, ip_address=client_ip(),
                 user_agent=user_agent(), metadata=metadata)


def _get_table(table_id: int) -> RestaurantTable:
    table = db.session.get(RestaurantTable, table_id)
    if table is None:
        abort(404)
    return table


def _flash_errors(exc: TableError) -> None:
    for messages in exc.errors.values():
        for message in messages:
            flash(message, "error")


@tables_bp.get("/staff/tables")
@require_role(Role.STAFF, Role.ADMIN)
def board():
    tables = list_tables()
    counts = {status: sum(t.status == status for t in tables) for status in TableStatus}
    return render_template(
        "tables/board.html",
        tables=tables,
        counts=counts,
        active_orders=active_orders_by_table(),
        transitions=ALLOWED_TABLE_TRANSITIONS,
        form=TableStatusForm(),
    )


@tables_bp.post("/staff/tables/<int:table_id>/status")
@require_role(Role.STAFF, Role.ADMIN)
def set_status(table_id: int):
    table = _get_table(table_id)
    form = TableStatusForm()
    if not form.validate_on_submit():
        flash("Could not update the table.", "error")
        return redirect(url_for("tables.board"))
    try:
        previous = change_table_status(table, form.status.data)
    except TableError as exc:
        _flash_errors(exc)
        return redirect(url_for("tables.board"))
    _audit(AuditEvent.TABLE_STATUS_CHANGED,
           {"table_id": table.id, "from": previous.value, "to": table.status.value})
    flash(f"{table.name} is now {table.status.value.replace('_', ' ')}.", "success")
    return redirect(url_for("tables.board"))


@tables_bp.route("/admin/tables/new", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def new():
    form = TableForm()
    if form.validate_on_submit():
        try:
            table = create_table(form.name.data, form.capacity.data)
        except TableError as exc:
            _flash_errors(exc)
        else:
            _audit(AuditEvent.TABLE_CREATED, {"table_id": table.id})
            flash(f"{table.name} added.", "success")
            return redirect(url_for("tables.board"))
    return render_template("tables/form.html", form=form, table=None)


@tables_bp.route("/admin/tables/<int:table_id>/edit", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def edit(table_id: int):
    table = _get_table(table_id)
    form = TableForm(obj=table)
    if form.validate_on_submit():
        try:
            update_table(table, form.name.data, form.capacity.data)
        except TableError as exc:
            db.session.refresh(table)
            _flash_errors(exc)
        else:
            _audit(AuditEvent.TABLE_UPDATED, {"table_id": table.id})
            flash(f"{table.name} updated.", "success")
            return redirect(url_for("tables.board"))
    return render_template("tables/form.html", form=form, table=table)
