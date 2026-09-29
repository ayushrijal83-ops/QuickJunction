"""Checkout and order history.

Checkout requires an authenticated customer (``@login_required``). Order
history and detail are scoped to the requesting user's own orders only --
ownership is enforced inside the database query itself
(app/services/orders.py::get_order_for_user), not checked after the fact.
That is what makes an IDOR attempt -- a logged-in customer editing another
account's order id into the URL -- return a plain 404 instead of leaking
whether the id exists or belongs to someone else.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, url_for

from app.models.audit_log import AuditEvent
from app.models.order import OrderSource, OrderStatus
from app.routes.forms import CancelOrderForm, CheckoutForm
from app.services.audit import record_event
from app.services.cart import build_cart_view
from app.services.orders import (
    CheckoutError,
    OrderStatusError,
    cancel_order_as_customer,
    checkout,
    customer_can_cancel,
    get_order_for_user,
    list_orders_for_user,
)
from app.services.pricing import preview
from app.services.tables import seatable_tables
from app.utils.authorization import get_current_user, login_required
from app.utils.cart import clear_cart, get_cart
from app.utils.errors import wants_html
from app.utils.request_meta import client_ip, user_agent

orders_bp = Blueprint("orders", __name__)


@orders_bp.get("/checkout")
@login_required
def checkout_page():
    cart_view = build_cart_view(get_cart())
    # Offered for dine-in only; the service re-checks the chosen table anyway.
    tables = seatable_tables()
    return render_template("orders/checkout.html", cart=cart_view, form=CheckoutForm(),
                           tables=tables, sources=list(OrderSource),
                           pricing_preview=preview(cart_view.subtotal))  # display only; checkout recomputes


@orders_bp.post("/checkout")
@login_required
def checkout_submit():
    form = CheckoutForm()
    user = get_current_user()

    if not form.validate_on_submit():
        flash("Could not place your order.", "error")
        return redirect(url_for("orders.checkout_page"))

    try:
        order = checkout(user.id, get_cart(), form.source.data, form.table_id.data)
    except CheckoutError as exc:
        record_event(
            AuditEvent.ORDER_CREATION_FAILED,
            success=False,
            user_id=user.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
        )
        for messages in exc.errors.values():
            for message in messages:
                flash(message, "error")
        return redirect(url_for("orders.checkout_page"))

    clear_cart()
    record_event(
        AuditEvent.ORDER_CREATED,
        success=True,
        user_id=user.id,
        ip_address=client_ip(),
        user_agent=user_agent(),
        metadata={"order_id": order.id, "total": str(order.total)},
    )
    flash("Order placed.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@orders_bp.get("/orders")
@login_required
def history():
    user = get_current_user()
    return render_template("orders/history.html", orders=list_orders_for_user(user.id))


@orders_bp.get("/orders/<int:order_id>")
@login_required
def detail(order_id: int):
    user = get_current_user()
    order = get_order_for_user(order_id, user.id)
    if order is None:
        abort(404)
    return render_template("orders/detail.html", order=order, cancel_form=CancelOrderForm(),
                           can_cancel=customer_can_cancel(order))


@orders_bp.post("/orders/<int:order_id>/cancel")
@login_required
def cancel(order_id: int):
    """Customer self-service cancellation. The button only renders while the
    order is cancellable, but that is convenience: the service enforces the
    status rule and ownership inside the UPDATE itself."""
    user = get_current_user()
    order = get_order_for_user(order_id, user.id)
    if order is None:
        abort(404)  # nonexistent and someone else's look identical

    form = CancelOrderForm()
    if not form.validate_on_submit():
        flash("Could not cancel the order.", "error")
        return redirect(url_for("orders.detail", order_id=order.id))

    previous = order.status
    try:
        previous = cancel_order_as_customer(order, user, form.reason.data)
    except OrderStatusError as exc:
        record_event(
            AuditEvent.ORDER_STATUS_CHANGE_REJECTED,
            success=False,
            user_id=user.id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"order_id": order.id, "from": previous.value, "attempted": "cancelled", "actor": "customer"},
        )
        message = exc.errors["status"][0]
        if not wants_html():
            return jsonify(error={"status": 409, "message": message}), 409
        flash(message, "error")
        return redirect(url_for("orders.detail", order_id=order.id))

    record_event(
        AuditEvent.ORDER_STATUS_CHANGED,
        success=True,
        user_id=user.id,
        ip_address=client_ip(),
        user_agent=user_agent(),
        metadata={"order_id": order.id, "from": previous.value, "to": OrderStatus.CANCELLED.value,
                  "actor": "customer"},
    )
    flash(f"Order #{order.id} has been cancelled.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))
