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
from app.models.order import Order, OrderSource, OrderStatus
from app.models.payment import Payment, PaymentMethod
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.models.restaurant_table import TableStatus
from app.models.user import Role
from app.services.orders import OrderStatusError, cancel_order_as_customer, update_order_status
from app.services.payment_ledger import LedgerPaymentError, record_ledger_payment, refund_ledger_payment
from app.services.sales import build_report
from app.services.tables import create_table
from app.utils.clock import db_today
from tests.conftest import csrf_token, make_legacy_payment, make_user, rejected_by_check_constraint
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


def test_payment_amount_is_server_validated_against_balance_due(client, db):
    """M7.5.3: unlike the retired one-full-payment-per-order route, the
    posted amount is now real (partial/multiple payments) -- but it is
    still re-validated server-side against the order's actual balance due,
    not trusted outright."""
    order = place(customer(), price="385.00", qty=3)
    cook = member()
    login(client, "cook")
    r = client.post(f"/staff/orders/{order.id}/payment", data={"method": "card", "amount": "1305.15"})
    assert r.status_code == 302
    entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT
    ).one()
    assert (entry.order_id, entry.method, entry.amount) == (order.id, PaymentMethod.CARD, D("1305.15"))  # 1155.00 + 13 % tax
    assert entry.recorded_by_id == cook.id and entry.created_at is not None
    assert _db.session.get(Order, order.id).amount_settled == D("1305.15")
    assert audit(AuditEvent.PAYMENT_RECORDED) == [
        {"order_id": order.id, "payment_ledger_entry_id": entry.id, "method": "card", "amount": "1305.15"}]


def test_payment_amount_overstating_the_balance_due_is_rejected(client, db):
    order = place(customer(), price="385.00", qty=3)
    member()
    login(client, "cook")
    client.post(f"/staff/orders/{order.id}/payment", data={"method": "card", "amount": "99999.00"})
    assert _db.session.query(PaymentLedgerEntry).count() == 0
    assert _db.session.get(Order, order.id).amount_settled == D("0.00")


def test_database_enforces_one_payment_per_order_and_refund_bounds(db):
    """The UNIQUE order_id / CHECK refund-bound constraints are schema-level
    guarantees that must hold regardless of which writer touches the table --
    app/services/payments.py (retired M7.5.4.7) used to be that writer; the
    row is now built directly via the make_legacy_payment test fixture."""
    order, cook = place(customer()), member()
    p = make_legacy_payment(order, cook)
    with pytest.raises(sa.exc.IntegrityError):
        _db.session.add(Payment(order_id=order.id, method=PaymentMethod.CASH, amount=D("1.00"), recorded_by_id=cook.id))
        _db.session.flush()
    _db.session.rollback()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("UPDATE payments SET refunded_amount = amount + 1 WHERE id = :i"), {"i": p.id})
        _db.session.flush()
    _db.session.rollback()


def test_payment_rbac_and_csrf(app, client, db):
    order = place(customer())
    data = {"method": "cash", "amount": str(order.total)}
    assert client.post(f"/staff/orders/{order.id}/payment", data=data).status_code == 401
    login(client, "diner")
    assert client.post(f"/staff/orders/{order.id}/payment", data=data).status_code == 403
    staff = app.test_client()
    member()
    login(staff, "cook")
    app.config["WTF_CSRF_ENABLED"] = True
    staff.post(f"/staff/orders/{order.id}/payment", data=data)
    assert _db.session.query(PaymentLedgerEntry).count() == 0
    token = csrf_token(staff, f"/staff/orders/{order.id}")
    staff.post(f"/staff/orders/{order.id}/payment", data={**data, "csrf_token": token})
    assert _db.session.query(PaymentLedgerEntry).count() == 1


# --- refunds ------------------------------------------------------------------------


def test_refund_is_admin_only(app, client, db):
    order = place(customer())
    p = record_ledger_payment(order, order.total, "cash", member())
    login(client, "cook")
    assert client.post(f"/staff/orders/{order.id}/payments/{p.id}/refund",
                       data={"amount": "10", "reason": "late"}).status_code == 403
    boss = app.test_client()
    member("boss", Role.ADMIN)
    login(boss, "boss")
    boss.post(f"/staff/orders/{order.id}/payments/{p.id}/refund", data={"amount": "10", "reason": "late"})
    _db.session.refresh(p)
    assert p.refunded_amount == D("10.00")
    assert audit(AuditEvent.PAYMENT_REFUNDED)[-1]["amount"] == "10.00"


# --- paid orders and cancellation ---------------------------------------------------


def test_paid_order_cannot_be_cancelled_until_fully_refunded(client, db):
    """M7.5.3: the cancellation guard (app/services/orders.py) now checks
    orders.amount_settled exclusively -- set up with the ledger-backed
    service, not the legacy record_payment/refund_payment."""
    user, cook = customer(), member()
    order = place(user)
    p = record_ledger_payment(order, order.total, "cash", cook)

    with pytest.raises(OrderStatusError):
        cancel_order_as_customer(order, user)
    login(client, "diner")
    assert "Cancel order" not in client.get(f"/orders/{order.id}").get_data(as_text=True)
    assert "Paid" in client.get(f"/orders/{order.id}").get_data(as_text=True)

    with pytest.raises(OrderStatusError) as exc:
        update_order_status(order, OrderStatus.CANCELLED, actor=member("boss", Role.ADMIN))
    assert "Refund the payment" in exc.value.errors["status"][0]
    refund_ledger_payment(p, "100.00", "partial refund", cook)  # partial: still holds money
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.CANCELLED)
    refund_ledger_payment(p, p.amount - p.refunded_amount, "the rest", cook)
    update_order_status(order, OrderStatus.CANCELLED)
    assert order.status == OrderStatus.CANCELLED


def test_payment_after_stale_cancel_view_is_refused(db):
    """Payment committed first; a cancel request that loaded the order earlier
    must still be refused -- the check is inside the UPDATE, against the
    live orders.amount_settled, not a stale in-memory copy of it."""
    user = customer()
    order = place(user)
    record_ledger_payment(order, order.total, "cash", member())
    set_committed_value(order, "amount_settled", D("0.00"))  # the cancelling request never saw the payment
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.CANCELLED)
    _db.session.expire_all()
    assert order.status == OrderStatus.PENDING


# --- reporting ------------------------------------------------------------------------


def test_report_figures_come_from_payments(db):
    """M7.5.3: build_report now reads the ledger, not the legacy payments
    table -- set up with the ledger-backed service."""
    today = db_today()
    user, cook = customer(), member()
    completed_paid = place(user, price="500.00", qty=1)
    completed_unpaid = place(user, price="300.00", qty=1)
    prepaid = place(user, price="200.00", qty=1)
    refunded_then_cancelled = place(user, price="999.00", qty=1)
    entries = {o.id: record_ledger_payment(o, o.total, "wallet", cook)
               for o in (completed_paid, prepaid, refunded_then_cancelled)}
    refund_ledger_payment(entries[completed_paid.id], "50.00", "missing side", cook)
    full = entries[refunded_then_cancelled.id]
    refund_ledger_payment(full, full.amount, "full refund", cook)
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
    # M3: checkout refuses to seat an occupied table, so two open orders on
    # one table can only be legacy data -- written directly here.
    second = place(user, source=OrderSource.DINE_IN, table_id=create_table("T05b", 4).id)
    second.table_id = table.id
    _db.session.commit()
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
