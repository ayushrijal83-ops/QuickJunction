"""Staff/admin order management.

Every route is gated by ``require_role(Role.STAFF, Role.ADMIN)`` -- the same
server-side decorator the admin menu routes use, reading the role from the
freshly-loaded ``User`` row on every request. Nothing here consults a
client-supplied role, header, hidden field, or template flag; a CUSTOMER
gets 403 and an anonymous visitor gets 401 before any view body runs.

ADMIN is not given a separate interface: it shares these routes and the
same service functions, since "admin may do what staff may do" is the whole
of the requirement.

Status changes go through ``app/services/orders.py``'s transition
allow-list. The template renders only the currently-legal buttons, but that
is a convenience -- the service re-validates the transition against the
order's real current status regardless of what was posted.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, url_for

from app.models.audit_log import AuditEvent
from app.models.user import Role
from app.models.payment import PaymentMethod
from app.routes.forms import OrderStatusForm, PaymentForm, RefundForm
from app.services.audit import record_event
from app.services.payments import PaymentError, record_payment, refund_payment
from app.services.orders import (
    OrderStatusError,
    allowed_next_statuses,
    coerce_status,
    get_order_for_staff,
    list_orders_for_staff,
    update_order_status,
)
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

staff_orders_bp = Blueprint("staff_orders", __name__, url_prefix="/staff")


@staff_orders_bp.get("/orders")
@require_role(Role.STAFF, Role.ADMIN)
def order_list():
    return render_template("staff/order_list.html", orders=list_orders_for_staff())


@staff_orders_bp.get("/orders/<int:order_id>")
@require_role(Role.STAFF, Role.ADMIN)
def order_detail(order_id: int):
    order = get_order_for_staff(order_id)
    if order is None:
        abort(404)
    return render_template(
        "staff/order_detail.html",
        order=order,
        form=OrderStatusForm(),
        next_statuses=allowed_next_statuses(order.status),
        payment_form=PaymentForm(),
        refund_form=RefundForm(),
        payment_methods=list(PaymentMethod),
    )


@staff_orders_bp.post("/orders/<int:order_id>/status")
@require_role(Role.STAFF, Role.ADMIN)
def order_status_update(order_id: int):
    order = get_order_for_staff(order_id)
    if order is None:
        abort(404)

    actor = get_current_user()
    form = OrderStatusForm()

    if not form.validate_on_submit():
        flash("Could not update the order status.", "error")
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))

    previous = order.status
    try:
        new_status = coerce_status(form.status.data)
        previous = update_order_status(order, new_status, actor=actor, reason=form.reason.data)
    except OrderStatusError as exc:
        # Rejected transitions are audited too: a repeated attempt to force
        # an order backwards is exactly the kind of thing worth seeing.
        record_event(
            AuditEvent.ORDER_STATUS_CHANGE_REJECTED,
            success=False,
            user_id=actor.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={
                "order_id": order.id,
                "from": previous.value,
                "attempted": (form.status.data or "")[:32],
            },
        )
        for messages in exc.errors.values():
            for message in messages:
                flash(message, "error")
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))

    record_event(
        AuditEvent.ORDER_STATUS_CHANGED,
        success=True,
        user_id=actor.id,
        ip_address=client_ip(),
        user_agent=user_agent(),
        metadata={"order_id": order.id, "from": previous.value, "to": order.status.value,
                  "actor": actor.role.value},
    )
    flash(f"Order #{order.id} is now {order.status.value}.", "success")
    return redirect(url_for("staff_orders.order_detail", order_id=order.id))


def _flash_payment_errors(exc: PaymentError) -> None:
    for messages in exc.errors.values():
        for message in messages:
            flash(message, "error")


@staff_orders_bp.post("/orders/<int:order_id>/payment")
@require_role(Role.STAFF, Role.ADMIN)
def record_order_payment(order_id: int):
    order = get_order_for_staff(order_id)
    if order is None:
        abort(404)
    form = PaymentForm()
    if not form.validate_on_submit():
        flash("Could not record the payment.", "error")
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))
    actor = get_current_user()
    try:
        payment = record_payment(order, form.method.data, actor)
    except PaymentError as exc:
        _flash_payment_errors(exc)
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))
    record_event(
        AuditEvent.PAYMENT_RECORDED,
        success=True,
        user_id=actor.id,
        ip_address=client_ip(),
        user_agent=user_agent(),
        metadata={"order_id": order.id, "payment_id": payment.id, "method": payment.method.value,
                  "amount": str(payment.amount)},
    )
    flash(f"Payment of {payment.amount} recorded for order #{order.id}.", "success")
    return redirect(url_for("staff_orders.order_detail", order_id=order.id))


@staff_orders_bp.post("/orders/<int:order_id>/refund")
@require_role(Role.ADMIN)
def refund_order_payment(order_id: int):
    """ADMIN only: money leaving the business is a manager's decision."""
    order = get_order_for_staff(order_id)
    if order is None or order.payment is None:
        abort(404)
    form = RefundForm()
    if not form.validate_on_submit():
        flash("Enter a refund amount.", "error")
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))
    try:
        amount = refund_payment(order.payment, form.amount.data, form.reason.data)
    except PaymentError as exc:
        _flash_payment_errors(exc)
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))
    record_event(
        AuditEvent.PAYMENT_REFUNDED,
        success=True,
        user_id=get_current_user().id,
        ip_address=client_ip(),
        user_agent=user_agent(),
        metadata={"order_id": order.id, "payment_id": order.payment.id, "amount": str(amount),
                  "refunded_total": str(order.payment.refunded_amount)},
    )
    flash(f"Refunded {amount} on order #{order.id}.", "success")
    return redirect(url_for("staff_orders.order_detail", order_id=order.id))
