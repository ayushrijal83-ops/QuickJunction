"""Table reservations: customer booking + staff reservation management.

Customer routes are ``customer_required``; the booking owner is always
``get_current_user()`` -- no route reads a user id from the request, so a
forged ``user_id`` field is simply never looked at. Reservation lookups for a
customer are ownership-scoped in the query (someone else's id -> 404).

Staff/admin see the customer's **username only** -- the same privacy
convention as the staff order queue (no email or other PII).
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.reservation import Reservation
from app.models.user import Role
from app.routes.forms import CancelOrderForm, ReservationForm, TableStatusForm
from app.services.audit import record_event
from app.services.reservations import (
    ALLOWED_RESERVATION_TRANSITIONS,
    SLOT_STARTS,
    ReservationError,
    available_tables,
    cancel_reservation_as_customer,
    create_reservation,
    customer_can_cancel,
    get_reservation_for_user,
    list_reservations_for_staff,
    list_reservations_for_user,
    parse_slot,
    update_reservation_status,
)
from app.utils.authorization import customer_required, get_current_user, require_role
from app.utils.clock import db_today
from app.utils.errors import wants_html
from app.utils.request_meta import client_ip, user_agent

reservations_bp = Blueprint("reservations", __name__)


def _audit(event: AuditEvent, success: bool, metadata: dict) -> None:
    user = get_current_user()
    record_event(event, success=success, user_id=user.id, ip_address=client_ip(), user_agent=user_agent(),
                 metadata={**metadata, "actor": user.role.value})


def _flash_errors(exc: ReservationError) -> None:
    for messages in exc.errors.values():
        for message in messages:
            flash(message, "error")


# --- customer --------------------------------------------------------------------


@reservations_bp.get("/reservations/new")
@customer_required
def new():
    """Step 1 (GET, bookmarkable): pick date/time/party size and see which
    tables are free for that slot. Step 2 posts the chosen table."""
    args = request.args
    tables, slot, errors = None, None, {}
    if args.get("reservation_date"):
        try:
            slot = parse_slot(args.get("reservation_date"), args.get("reservation_time"), args.get("guest_count"))
            tables = available_tables(*slot)
        except ReservationError as exc:
            errors = exc.errors
    return render_template("reservations/new.html", slots=SLOT_STARTS, args=args, slot=slot,
                           tables=tables, errors=errors, form=ReservationForm())


@reservations_bp.post("/reservations")
@customer_required
def create():
    form = ReservationForm()
    if not form.validate_on_submit():
        flash("Please choose a date, time, party size and table.", "error")
        return redirect(url_for("reservations.new"))
    try:
        reservation = create_reservation(get_current_user(), form.table_id.data, form.reservation_date.data,
                                         form.reservation_time.data, form.guest_count.data)
    except ReservationError as exc:
        if not wants_html():
            return jsonify(error={"status": 409, "message": next(iter(exc.errors.values()))[0]}), 409
        _flash_errors(exc)
        return redirect(url_for("reservations.new", reservation_date=form.reservation_date.data,
                                reservation_time=form.reservation_time.data, guest_count=form.guest_count.data))
    _audit(AuditEvent.RESERVATION_CREATED, True, {"reservation_id": reservation.id, "table_id": reservation.table_id})
    flash("Reservation confirmed.", "success")
    return redirect(url_for("reservations.detail", reservation_id=reservation.id))


@reservations_bp.get("/reservations")
@customer_required
def mine():
    return render_template("reservations/mine.html", reservations=list_reservations_for_user(get_current_user().id))


@reservations_bp.get("/reservations/<int:reservation_id>")
@customer_required
def detail(reservation_id: int):
    reservation = get_reservation_for_user(reservation_id, get_current_user().id)
    if reservation is None:
        abort(404)
    return render_template("reservations/detail.html", r=reservation, form=CancelOrderForm(),
                           can_cancel=customer_can_cancel(reservation))


@reservations_bp.post("/reservations/<int:reservation_id>/cancel")
@customer_required
def cancel(reservation_id: int):
    user = get_current_user()
    reservation = get_reservation_for_user(reservation_id, user.id)
    if reservation is None:
        abort(404)
    if not CancelOrderForm().validate_on_submit():
        abort(400)
    try:
        previous = cancel_reservation_as_customer(reservation, user)
    except ReservationError as exc:
        _audit(AuditEvent.RESERVATION_STATUS_CHANGED, False,
               {"reservation_id": reservation.id, "from": reservation.status.value, "attempted": "cancelled"})
        if not wants_html():
            return jsonify(error={"status": 409, "message": exc.errors["status"][0]}), 409
        _flash_errors(exc)
        return redirect(url_for("reservations.detail", reservation_id=reservation.id))
    _audit(AuditEvent.RESERVATION_STATUS_CHANGED, True,
           {"reservation_id": reservation.id, "from": previous.value, "to": reservation.status.value})
    flash("Reservation cancelled.", "success")
    return redirect(url_for("reservations.detail", reservation_id=reservation.id))


# --- staff / admin ---------------------------------------------------------------


@reservations_bp.get("/staff/reservations")
@require_role(Role.STAFF, Role.ADMIN)
def staff_list():
    return render_template("reservations/staff_list.html", reservations=list_reservations_for_staff(db_today()),
                           transitions=ALLOWED_RESERVATION_TRANSITIONS, form=TableStatusForm())


@reservations_bp.post("/staff/reservations/<int:reservation_id>/status")
@require_role(Role.STAFF, Role.ADMIN)
def staff_set_status(reservation_id: int):
    reservation = db.session.get(Reservation, reservation_id)
    if reservation is None:
        abort(404)
    form = TableStatusForm()  # one CSRF-protected `status` field; reused rather than duplicated
    if not form.validate_on_submit():
        abort(400)
    try:
        previous = update_reservation_status(reservation, form.status.data)
    except ReservationError as exc:
        _flash_errors(exc)
        return redirect(url_for("reservations.staff_list"))
    _audit(AuditEvent.RESERVATION_STATUS_CHANGED, True,
           {"reservation_id": reservation.id, "from": previous.value, "to": reservation.status.value})
    flash(f"Reservation #{reservation.id} is now {reservation.status.value.replace('_', ' ')}.", "success")
    return redirect(url_for("reservations.staff_list"))
