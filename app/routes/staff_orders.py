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

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.models.audit_log import AuditEvent
from app.models.user import Role
from app.models.payment import PaymentMethod
from app.routes.forms import DiscountForm, OrderStatusForm, PaymentForm, RefundForm, StaffOrderForm
from app.services.audit import record_event
from app.services.cart import MAX_QUANTITY_PER_ITEM, MIN_QUANTITY, build_cart_view
from app.services.menu import list_public_menu_items
from app.services.payments import PaymentError, record_payment, refund_payment
from app.services.pricing import PricingError, apply_discount, current_settings, preview
from app.services.orders import (
    STAFF_ORDER_SOURCES,
    CheckoutError,
    OrderStatusError,
    allowed_next_statuses,
    coerce_status,
    get_order_for_staff,
    list_orders_for_staff,
    place_staff_order,
    update_order_status,
)
from app.services.tables import seatable_tables
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

staff_orders_bp = Blueprint("staff_orders", __name__, url_prefix="/staff")


@staff_orders_bp.get("/orders")
@require_role(Role.STAFF, Role.ADMIN)
def order_list():
    return render_template("staff/order_list.html", orders=list_orders_for_staff())


_QTY_FIELD = re.compile(r"^qty_(\d{1,9})$")
_QTY_VALUE = re.compile(r"^\d{1,6}$")


def _posted_cart(form_data) -> tuple[dict[str, int], list[str]]:
    """``qty_<menu item id>`` fields -> the same {id: quantity} cart shape the
    customer checkout uses. Blank means "not ordered"; anything else must be
    a whole number in the existing per-item bounds. Every other posted field
    (a price, a total, ...) is simply never read."""
    cart: dict[str, int] = {}
    errors: list[str] = []
    for key, raw in form_data.items():
        match = _QTY_FIELD.match(key)
        if match is None or not raw.strip():
            continue
        raw = raw.strip()
        quantity = int(raw) if _QTY_VALUE.match(raw) else None
        if quantity is None or not MIN_QUANTITY <= quantity <= MAX_QUANTITY_PER_ITEM:
            errors.append(f"Quantities must be whole numbers from {MIN_QUANTITY} to {MAX_QUANTITY_PER_ITEM}.")
            continue
        cart[match.group(1)] = quantity
    if not cart and not errors:
        errors.append("Add at least one item.")
    return cart, errors[:1]


def _order_entry_page(form: StaffOrderForm, cart: dict[str, int], review: bool = False):
    view = build_cart_view(cart) if review else None
    return render_template(
        "staff/order_new.html",
        form=form,
        items=list_public_menu_items(),
        tables=seatable_tables(),
        sources=sorted(STAFF_ORDER_SOURCES, key=lambda s: s.value),
        cart=cart,
        max_qty=MAX_QUANTITY_PER_ITEM,
        view=view,
        pricing_preview=preview(view.subtotal) if view else None,  # display only; checkout recomputes
    )


@staff_orders_bp.route("/orders/new", methods=["GET", "POST"])
@require_role(Role.STAFF, Role.ADMIN)
def order_new():
    """Staff order entry (M3): the controlled path for dine-in, plus
    takeaway. One stateless form -- the menu with a quantity box per dish.
    "Review" re-renders it with server-computed totals and writes nothing;
    "Create order" goes through ``place_staff_order`` -> ``checkout``, which
    re-reads every price, checks availability and stock, locks and seats the
    table, and commits the ORDER_CREATED audit row in the same transaction."""
    form = StaffOrderForm()
    if request.method == "GET":
        return _order_entry_page(form, {})
    if not form.validate_on_submit():
        flash("Could not read the order. Please try again.", "error")
        return redirect(url_for("staff_orders.order_new"))

    cart, errors = _posted_cart(request.form)
    if errors:
        for message in errors:
            flash(message, "error")
        return _order_entry_page(form, cart), 400
    if request.form.get("action") != "create":
        return _order_entry_page(form, cart, review=True)

    actor = get_current_user()

    def audit(order):
        return record_event(
            AuditEvent.ORDER_CREATED,
            success=True,
            user_id=actor.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"order_id": order.id, "total": str(order.total), "source": order.source.value,
                      "table_id": order.table_id, "actor": actor.role.value},
        )

    try:
        order = place_staff_order(actor, cart, form.source.data, form.table_id.data, audit=audit)
    except CheckoutError as exc:
        record_event(
            AuditEvent.ORDER_CREATION_FAILED,
            success=False,
            user_id=actor.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"actor": actor.role.value},
            commit=True,  # independent: nothing was written
        )
        for messages in exc.errors.values():
            for message in messages:
                flash(message, "error")
        return _order_entry_page(form, cart), 409

    where = f" for {order.table.name}" if order.table else ""
    flash(f"Order #{order.id} created{where}. Total {order.total}.", "success")
    return redirect(url_for("staff_orders.order_detail", order_id=order.id))


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
        discount_form=DiscountForm(),
        staff_max_discount=current_settings()[1],
    )


@staff_orders_bp.post("/orders/<int:order_id>/discount")
@require_role(Role.STAFF, Role.ADMIN)
def discount_order(order_id: int):
    """STAFF within the configured cap, ADMIN up to 100 % -- the cap is
    enforced in app/services/pricing.py from the actor's server-side role."""
    order = get_order_for_staff(order_id)
    if order is None:
        abort(404)
    form = DiscountForm()
    if not form.validate_on_submit():
        flash("Could not apply the discount.", "error")
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))
    actor = get_current_user()

    def audit(order):
        return record_event(
            AuditEvent.ORDER_DISCOUNT_APPLIED,
            success=True,
            user_id=actor.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"order_id": order.id,
                      "type": order.discount_type.value if order.discount_type else None,
                      "value": str(order.discount_value) if order.discount_value is not None else None,
                      "amount": str(order.discount_amount), "total": str(order.total)},
        )

    try:
        order = apply_discount(order, form.discount_type.data, form.discount_value.data,
                               form.discount_reason.data, actor, audit=audit)
    except PricingError as exc:
        for messages in exc.errors.values():
            for message in messages:
                flash(message, "error")
        return redirect(url_for("staff_orders.order_detail", order_id=order_id))
    flash(f"Order #{order.id} now totals {order.total}.", "success")
    return redirect(url_for("staff_orders.order_detail", order_id=order.id))


@staff_orders_bp.post("/orders/<int:order_id>/status")
@require_role(Role.STAFF, Role.ADMIN)
def order_status_update(order_id: int):
    order = get_order_for_staff(order_id)
    if order is None:
        abort(404)

    actor = get_current_user()
    form = OrderStatusForm()
    # Where to go afterwards. An allow-list of one named page, never a URL, so
    # this cannot be turned into an open redirect.
    back = (redirect(url_for("kitchen.board")) if request.form.get("return_to") == "kitchen"
            else redirect(url_for("staff_orders.order_detail", order_id=order.id)))

    if not form.validate_on_submit():
        flash("Could not update the order status.", "error")
        return back

    previous = order.status

    def audit():
        return record_event(
            AuditEvent.ORDER_STATUS_CHANGED,
            success=True,
            user_id=actor.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"order_id": order.id, "from": previous.value, "to": new_status.value,
                      "actor": actor.role.value},
        )

    try:
        new_status = coerce_status(form.status.data)
        update_order_status(order, new_status, actor=actor, reason=form.reason.data, audit=audit)
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
            commit=True,  # independent: the rejected change wrote nothing
        )
        for messages in exc.errors.values():
            for message in messages:
                flash(message, "error")
        return back

    flash(f"Order #{order.id} is now {order.status.value}.", "success")
    return back


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

    def audit(payment):
        return record_event(
            AuditEvent.PAYMENT_RECORDED,
            success=True,
            user_id=actor.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"order_id": order.id, "payment_id": payment.id, "method": payment.method.value,
                      "amount": str(payment.amount)},
        )

    try:
        payment = record_payment(order, form.method.data, actor, audit=audit)
    except PaymentError as exc:
        _flash_payment_errors(exc)
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))
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
    def audit(amount):
        return record_event(
            AuditEvent.PAYMENT_REFUNDED,
            success=True,
            user_id=get_current_user().id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"order_id": order.id, "payment_id": order.payment.id, "amount": str(amount),
                      "refunded_total": str(order.payment.refunded_amount)},
        )

    try:
        amount = refund_payment(order.payment, form.amount.data, form.reason.data, audit=audit)
    except PaymentError as exc:
        _flash_payment_errors(exc)
        return redirect(url_for("staff_orders.order_detail", order_id=order.id))
    flash(f"Refunded {amount} on order #{order.id}.", "success")
    return redirect(url_for("staff_orders.order_detail", order_id=order.id))
