"""Milestone 11: payments, refunds, the paid-order cancellation rule, the
payment-derived report figures and automatic table release."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import sqlalchemy as sa
from flask import g
from sqlalchemy.orm.attributes import set_committed_value

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import OrderSource, OrderStatus
from app.models.payment import Payment, PaymentMethod
from app.models.restaurant_table import TableStatus
from app.models.user import Role
from app.services.orders import OrderStatusError, cancel_order_as_customer, update_order_status
from app.services.payments import PaymentError, record_payment, refund_payment
from app.services.sales import build_report
from app.services.tables import create_table
from app.utils.clock import db_today
from tests.conftest import csrf_token, make_user, rejected_by_check_constraint
from tests.test_restaurant_ops import customer, place, set_status

PASSWORD = "correct-horse-1"
D = Decimal


def member(name="cook", role=Role.STAFF):
    return make_user(username=name, email=f"{name}@example.com", password=PASSWORD, role=role)


def login(client, name):
    g.pop("current_user", None)  # known issue #29
    client.post("/login", data={"username": name, "password": PASSWORD})


def audit(event):
    return [json.loads(r.metadata_json) for r in _db.session.query(AuditLog).filter_by(event_type=event)]


# --- recording --------------------------------------------------------------------


def test_payment_amount_is_the_server_side_total(client, db):
    order = place(customer(), price="385.00", qty=3)
    cook = member()
    login(client, "cook")
    r = client.post(f"/staff/orders/{order.id}/payment", data={"method": "card", "amount": "1.00", "total": "1.00"})
    assert r.status_code == 302
    p = _db.session.query(Payment).one()
    assert (p.order_id, p.method, p.amount, p.refunded_amount) == (order.id, PaymentMethod.CARD, D("1305.15"), D("0.00"))  # 1155.00 + 13 % tax
    assert p.recorded_by_id == cook.id and p.captured_at is not None
    assert audit(AuditEvent.PAYMENT_RECORDED) == [
        {"order_id": order.id, "payment_id": p.id, "method": "card", "amount": "1305.15"}]


def test_invalid_method_and_double_payment_rejected(db):
    order, cook = place(customer()), member()
    with pytest.raises(PaymentError):
        record_payment(order, "bitcoin", cook)
    record_payment(order, "cash", cook)
    with pytest.raises(PaymentError) as exc:
        record_payment(order, "cash", cook)
    assert "already paid" in exc.value.errors["payment"][0]
    assert _db.session.query(Payment).count() == 1


def test_database_enforces_one_payment_per_order_and_refund_bounds(db):
    order, cook = place(customer()), member()
    p = record_payment(order, "cash", cook)
    with pytest.raises(sa.exc.IntegrityError):
        _db.session.add(Payment(order_id=order.id, method=PaymentMethod.CASH, amount=D("1.00"), recorded_by_id=cook.id))
        _db.session.flush()
    _db.session.rollback()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("UPDATE payments SET refunded_amount = amount + 1 WHERE id = :i"), {"i": p.id})
        _db.session.flush()
    _db.session.rollback()


def test_cancelled_order_cannot_be_paid(db):
    user = customer()
    order = place(user)
    cancel_order_as_customer(order, user)
    with pytest.raises(PaymentError):
        record_payment(order, "cash", member())


def test_payment_rbac_and_csrf(app, client, db):
    order = place(customer())
    assert client.post(f"/staff/orders/{order.id}/payment", data={"method": "cash"}).status_code == 401
    login(client, "diner")
    assert client.post(f"/staff/orders/{order.id}/payment", data={"method": "cash"}).status_code == 403
    staff = app.test_client()
    member()
    login(staff, "cook")
    app.config["WTF_CSRF_ENABLED"] = True
    staff.post(f"/staff/orders/{order.id}/payment", data={"method": "cash"})
    assert _db.session.query(Payment).count() == 0
    token = csrf_token(staff, f"/staff/orders/{order.id}")
    staff.post(f"/staff/orders/{order.id}/payment", data={"method": "cash", "csrf_token": token})
    assert _db.session.query(Payment).count() == 1


# --- refunds ------------------------------------------------------------------------


def test_partial_then_full_refund_never_exceeds_amount(db):
    p = record_payment(place(customer(), price="100.00", qty=2), "cash", member())
    assert p.amount == D("226.00")  # 200.00 + 13 % tax
    assert refund_payment(p, "50", "cold fries") == D("50.00")
    assert (p.refunded_amount, p.refund_reason, p.fully_refunded) == (D("50.00"), "cold fries", False)
    with pytest.raises(PaymentError):
        refund_payment(p, "176.01", None)
    refund_payment(p, "176.00", None)
    assert p.fully_refunded and p.net_amount == D("0.00")
    with pytest.raises(PaymentError):
        refund_payment(p, "0.01", None)


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "NaN", "Infinity", ""])
def test_invalid_refund_amounts(db, amount):
    p = record_payment(place(customer()), "cash", member())
    with pytest.raises(PaymentError):
        refund_payment(p, amount, None)
    assert p.refunded_amount == D("0.00")


def test_stale_concurrent_refund_cannot_overdraw(db):
    """Two admins both read 'nothing refunded yet' and each refund the full
    amount; the conditional UPDATE lets only the first through."""
    p = record_payment(place(customer(), price="100.00", qty=1), "cash", member())
    refund_payment(p, "100.00", None)
    set_committed_value(p, "refunded_amount", D("0.00"))  # the second admin's stale view
    with pytest.raises(PaymentError):
        refund_payment(p, "100.00", None)
    assert p.refunded_amount == D("100.00")


def test_refund_is_admin_only(app, client, db):
    order = place(customer())
    p = record_payment(order, "cash", member())
    login(client, "cook")
    assert client.post(f"/staff/orders/{order.id}/refund", data={"amount": "10"}).status_code == 403
    boss = app.test_client()
    member("boss", Role.ADMIN)
    login(boss, "boss")
    boss.post(f"/staff/orders/{order.id}/refund", data={"amount": "10", "reason": "late"})
    _db.session.refresh(p)
    assert p.refunded_amount == D("10.00")
    assert audit(AuditEvent.PAYMENT_REFUNDED)[-1]["refunded_total"] == "10.00"


# --- paid orders and cancellation ---------------------------------------------------


def test_paid_order_cannot_be_cancelled_until_fully_refunded(client, db):
    user = customer()
    order = place(user)
    p = record_payment(order, "cash", member())

    with pytest.raises(OrderStatusError):
        cancel_order_as_customer(order, user)
    login(client, "diner")
    assert "Cancel order" not in client.get(f"/orders/{order.id}").get_data(as_text=True)
    assert "Paid" in client.get(f"/orders/{order.id}").get_data(as_text=True)

    with pytest.raises(OrderStatusError) as exc:
        update_order_status(order, OrderStatus.CANCELLED, actor=member("boss", Role.ADMIN))
    assert "Refund the payment" in exc.value.errors["status"][0]
    refund_payment(p, "100.00", None)  # partial: still holds money
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.CANCELLED)
    refund_payment(p, p.net_amount, None)
    update_order_status(order, OrderStatus.CANCELLED)
    assert order.status == OrderStatus.CANCELLED


def test_payment_after_stale_cancel_view_is_refused(db):
    """Payment committed first; a cancel request that loaded the order earlier
    must still be refused -- the check is inside the UPDATE."""
    user = customer()
    order = place(user)
    record_payment(order, "cash", member())
    set_committed_value(order, "payment", None)  # the cancelling request never saw it
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.CANCELLED)
    _db.session.expire_all()
    assert order.status == OrderStatus.PENDING


# --- reporting ------------------------------------------------------------------------


def test_report_figures_come_from_payments(db):
    today = db_today()
    user, cook = customer(), member()
    completed_paid = place(user, price="500.00", qty=1)
    completed_unpaid = place(user, price="300.00", qty=1)
    prepaid = place(user, price="200.00", qty=1)
    refunded_then_cancelled = place(user, price="999.00", qty=1)
    for o in (completed_paid, prepaid, refunded_then_cancelled):
        record_payment(o, "wallet", cook)
    refund_payment(completed_paid.payment, "50.00", "missing side")
    refund_payment(refunded_then_cancelled.payment, str(refunded_then_cancelled.payment.amount), None)
    update_order_status(refunded_then_cancelled, OrderStatus.CANCELLED)
    for o in (completed_paid, completed_unpaid):
        set_status(o, OrderStatus.COMPLETED)

    report = build_report(today, today, "Today")
    t = report.totals
    assert (t.orders, t.gross, t.refunds, t.net) == (2, D("800.00"), D("50.00"), D("750.00"))
    # payments include 13 % tax (M12): 565.00 - 50.00 refunded; prepaid 226.00
    assert (t.paid, t.collected) == (1, D("515.00"))
    assert (report.unpaid_completed, report.prepaid, report.cancelled) == (1, D("226.00"), 1)
    assert report.totals.average == D("375.00")


# --- automatic table release -------------------------------------------------------


def test_table_released_only_when_last_active_order_finishes(db):
    user = customer()
    table = create_table("T05", 4)
    first = place(user, source=OrderSource.DINE_IN, table_id=table.id)
    second = place(user, source=OrderSource.DINE_IN, table_id=table.id)
    set_status(first, OrderStatus.READY)
    update_order_status(first, OrderStatus.COMPLETED)
    assert table.status == TableStatus.OCCUPIED  # second party still eating
    cancel_order_as_customer(second, user)
    _db.session.refresh(table)
    assert table.status == TableStatus.CLEANING


def test_release_never_overrides_a_non_occupied_table(db):
    user = customer()
    table = create_table("T06", 4)
    order = place(user, source=OrderSource.DINE_IN, table_id=table.id)
    table.status = TableStatus.RESERVED  # staff changed it meanwhile
    _db.session.commit()
    cancel_order_as_customer(order, user)
    _db.session.refresh(table)
    assert table.status == TableStatus.RESERVED


def test_money_is_decimal_not_float(db):
    p = record_payment(place(customer(), price="0.10", qty=3), "cash", member())
    assert isinstance(p.amount, Decimal) and p.amount == D("0.34")  # 3 x 0.10 + 0.039 tax -> 0.04
