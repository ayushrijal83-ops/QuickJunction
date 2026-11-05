"""Milestone 7.5.2/7.5.3: ledger-backed payment recording and refunds
(app/services/payment_ledger.py) -- multiple/partial payments and refunds
against payment_ledger_entries, the source of truth for all new financial
writes. M7.5.3 cut app/routes/staff_orders.py over to this module; the
legacy app/services/payments.py remains only for pre-cutover historical
data and is no longer called by any route."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy.orm.attributes import set_committed_value

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order, OrderStatus
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.services.orders import cancel_order_as_customer
from app.services.payment_ledger import (
    LedgerPaymentError,
    get_payment_entry_for_order,
    record_ledger_payment,
    refund_ledger_payment,
)
from tests.test_payments import member
from tests.test_restaurant_ops import customer, place

D = Decimal


# --- recording: multiple/partial payments -------------------------------------------


def test_multiple_partial_payments_accumulate_to_exact_settlement(db):
    order, cashier = place(customer(), price="100.00", qty=2), member()
    total = order.total
    half = (total / 2).quantize(D("0.01"))
    remainder = total - half

    first = record_ledger_payment(order, str(half), "cash", cashier)
    assert order.amount_settled == half
    assert (first.entry_type, first.amount, first.method.value) == (LedgerEntryType.PAYMENT, half, "cash")

    second = record_ledger_payment(order, str(remainder), "card", cashier)
    assert order.amount_settled == total
    assert second.amount == remainder

    balance_due = order.total - order.amount_settled
    assert balance_due == D("0.00")
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 2


def test_overpayment_rejected(db):
    order, cashier = place(customer(), price="50.00", qty=1), member()
    record_ledger_payment(order, order.total, "cash", cashier)
    with pytest.raises(LedgerPaymentError) as exc:
        record_ledger_payment(order, "0.01", "cash", cashier)
    assert "exceeds the balance due" in exc.value.errors["payment"][0]
    assert order.amount_settled == order.total


def test_overpayment_rejected_on_first_partial_payment_too_large(db):
    order, cashier = place(customer(), price="50.00", qty=1), member()
    with pytest.raises(LedgerPaymentError):
        record_ledger_payment(order, order.total + D("0.01"), "cash", cashier)
    assert order.amount_settled == D("0.00")
    assert _db.session.query(PaymentLedgerEntry).count() == 0


@pytest.mark.parametrize("amount", ["0", "-5", "0.00", "abc", "NaN", "Infinity", ""])
def test_invalid_payment_amount_rejected(db, amount):
    order, cashier = place(customer()), member()
    with pytest.raises(LedgerPaymentError):
        record_ledger_payment(order, amount, "cash", cashier)
    assert order.amount_settled == D("0.00")
    assert _db.session.query(PaymentLedgerEntry).count() == 0


def test_invalid_payment_method_rejected(db):
    order, cashier = place(customer()), member()
    with pytest.raises(LedgerPaymentError):
        record_ledger_payment(order, "10.00", "bitcoin", cashier)
    assert _db.session.query(PaymentLedgerEntry).count() == 0


def test_cancelled_order_cannot_be_paid_via_ledger(db):
    user = customer()
    order = place(user)
    cancel_order_as_customer(order, user)
    with pytest.raises(LedgerPaymentError) as exc:
        record_ledger_payment(order, "10.00", "cash", member())
    assert "cancelled order" in exc.value.errors["payment"][0]


def test_payment_records_acting_user(db):
    order, cashier = place(customer()), member()
    entry = record_ledger_payment(order, order.total, "wallet", cashier)
    assert entry.recorded_by_id == cashier.id


def test_payment_creates_exactly_the_expected_audit_event(db):
    order, cashier = place(customer(), price="40.00", qty=1), member()
    entry = record_ledger_payment(order, order.total, "cash", cashier)

    rows = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.PAYMENT_RECORDED).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id, row.success) == (cashier.id, True)
    assert json.loads(row.metadata_json) == {
        "order_id": order.id, "payment_ledger_entry_id": entry.id, "method": "cash", "amount": str(order.total),
    }


def test_payment_rejection_writes_no_audit_row(db):
    order, cashier = place(customer(), price="10.00", qty=1), member()
    with pytest.raises(LedgerPaymentError):
        record_ledger_payment(order, order.total + D("1.00"), "cash", cashier)
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.PAYMENT_RECORDED).count() == 0


def test_payment_audit_event_cannot_be_suppressed_or_replaced(db):
    """There is no audit= parameter at all -- a caller cannot omit, swap,
    or no-op the required event the way the pre-correction callback-based
    design allowed."""
    order, cashier = place(customer()), member()
    with pytest.raises(TypeError):
        record_ledger_payment(order, order.total, "cash", cashier, audit=lambda *a, **k: None)
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.PAYMENT_RECORDED).count() == 0


def test_payment_audit_failure_rolls_back_the_financial_operation(db, monkeypatch):
    import app.services.payment_ledger as payment_ledger_module

    def boom(*_a, **_k):
        raise RuntimeError("audit backend unavailable")

    monkeypatch.setattr(payment_ledger_module, "record_event", boom)

    order, cashier = place(customer(), price="20.00", qty=1), member()
    with pytest.raises(RuntimeError):
        record_ledger_payment(order, order.total, "cash", cashier)
    _db.session.rollback()
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 0
    assert _db.session.get(Order, order.id).amount_settled == D("0.00")


# --- refunds -----------------------------------------------------------------------


def test_partial_then_full_refund_never_exceeds_amount(db):
    order, cashier = place(customer(), price="100.00", qty=2), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    remaining = payment.amount - D("50.00")

    refund_ledger_payment(payment, "50", "cold fries", cashier)
    assert (payment.refunded_amount, order.amount_settled) == (D("50.00"), remaining)

    with pytest.raises(LedgerPaymentError):
        refund_ledger_payment(payment, remaining + D("0.01"), "too much", cashier)

    refund_ledger_payment(payment, remaining, "the rest", cashier)
    assert payment.refunded_amount == payment.amount
    assert order.amount_settled == D("0.00")

    with pytest.raises(LedgerPaymentError):
        refund_ledger_payment(payment, "0.01", "already refunded", cashier)


def test_multiple_refunds_against_one_payment(db):
    order, cashier = place(customer(), price="90.00", qty=1), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    total = payment.amount

    r1 = refund_ledger_payment(payment, "20.00", "first", cashier)
    r2 = refund_ledger_payment(payment, "30.00", "second", cashier)
    assert (r1.amount, r2.amount) == (D("-20.00"), D("-30.00"))
    assert (r1.reversed_entry_id, r2.reversed_entry_id) == (payment.id, payment.id)
    assert payment.refunded_amount == D("50.00")
    assert order.amount_settled == total - D("50.00")
    refunds = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.REFUND
    ).count()
    assert refunds == 2


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "NaN", "Infinity", ""])
def test_invalid_refund_amount_rejected(db, amount):
    order, cashier = place(customer()), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    with pytest.raises(LedgerPaymentError):
        refund_ledger_payment(payment, amount, "reason", cashier)
    assert payment.refunded_amount == D("0.00")


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_refund_requires_a_nonempty_reason(db, reason):
    order, cashier = place(customer()), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    with pytest.raises(LedgerPaymentError) as exc:
        refund_ledger_payment(payment, "1.00", reason, cashier)
    assert "reason" in exc.value.errors["payment"][0]
    assert payment.refunded_amount == D("0.00")


def test_over_refund_rejected(db):
    order, cashier = place(customer(), price="20.00", qty=1), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    with pytest.raises(LedgerPaymentError) as exc:
        refund_ledger_payment(payment, payment.amount + D("0.01"), "too much", cashier)
    assert "exceeds what remains refundable" in exc.value.errors["payment"][0]
    assert payment.refunded_amount == D("0.00")


def test_only_a_payment_entry_can_be_refunded(db):
    order, cashier = place(customer()), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    refund = refund_ledger_payment(payment, "1.00", "test", cashier)
    with pytest.raises(LedgerPaymentError) as exc:
        refund_ledger_payment(refund, "1.00", "double refund", cashier)
    assert "Only a payment entry" in exc.value.errors["payment"][0]


def test_get_payment_entry_for_order_does_not_return_a_refund_entry(db):
    """H2 correction: the refund route's lookup must not hand back a
    non-payment entry (e.g. a refund's own id) just because it belongs to
    the right order -- refund_ledger_payment's own entry_type check is a
    second line of defense, not the only one."""
    order, cashier = place(customer()), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    refund = refund_ledger_payment(payment, "1.00", "test", cashier)

    assert get_payment_entry_for_order(order.id, payment.id) is payment
    assert get_payment_entry_for_order(order.id, refund.id) is None


def test_refund_records_acting_user_and_reason(db):
    order, cashier = place(customer()), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    refund = refund_ledger_payment(payment, "5.00", "complaint", cashier)
    assert (refund.recorded_by_id, refund.reason) == (cashier.id, "complaint")


def test_refund_creates_exactly_the_expected_audit_event(db):
    order, cashier = place(customer(), price="60.00", qty=1), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    refund = refund_ledger_payment(payment, "15.00", "late", cashier)

    rows = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.PAYMENT_REFUNDED).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id, row.success) == (cashier.id, True)
    assert json.loads(row.metadata_json) == {
        "order_id": order.id, "payment_ledger_entry_id": refund.id, "reversed_entry_id": payment.id,
        "amount": "15.00", "reason": "late",
    }


def test_refund_audit_event_cannot_be_suppressed_or_replaced(db):
    order, cashier = place(customer()), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    with pytest.raises(TypeError):
        refund_ledger_payment(payment, "1.00", "test", cashier, audit=lambda *a, **k: None)
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.PAYMENT_REFUNDED).count() == 0


def test_refund_audit_failure_rolls_back_the_financial_operation(db, monkeypatch):
    import app.services.payment_ledger as payment_ledger_module

    order, cashier = place(customer(), price="25.00", qty=1), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)

    def boom(*_a, **_k):
        raise RuntimeError("audit backend unavailable")

    monkeypatch.setattr(payment_ledger_module, "record_event", boom)

    with pytest.raises(RuntimeError):
        refund_ledger_payment(payment, "5.00", "test", cashier)
    _db.session.rollback()
    _db.session.refresh(payment)
    assert payment.refunded_amount == D("0.00")
    assert _db.session.get(Order, order.id).amount_settled == payment.amount
    assert _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.REFUND
    ).count() == 0


def test_rejected_refund_rolls_back_projection_balance_and_audit(db):
    """A failed refund (over-refund) must change nothing: not the payment's
    refunded_amount projection, not the order's amount_settled, not the
    audit trail -- all three commit together or none does."""
    order, cashier = place(customer(), price="30.00", qty=1), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    settled_before = order.amount_settled

    with pytest.raises(LedgerPaymentError):
        refund_ledger_payment(payment, payment.amount + D("1.00"), "too much", cashier)

    assert payment.refunded_amount == D("0.00")
    assert order.amount_settled == settled_before
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.PAYMENT_REFUNDED).count() == 0
    assert _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.REFUND
    ).count() == 0


def test_stale_concurrent_refund_cannot_overdraw(db):
    """Two admins both read 'nothing refunded yet' and each refund the full
    amount; the conditional UPDATE lets only the first through, regardless
    of what the losing caller's in-memory object still believes."""
    order, cashier = place(customer(), price="75.00", qty=1), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    refund_ledger_payment(payment, payment.amount, "first", cashier)
    set_committed_value(payment, "refunded_amount", D("0.00"))  # the second admin's stale view
    with pytest.raises(LedgerPaymentError):
        refund_ledger_payment(payment, payment.amount, "second (stale)", cashier)
    assert payment.refunded_amount == payment.amount
    assert order.amount_settled == D("0.00")


def test_payment_after_stale_order_balance_view_cannot_overpay(db):
    """The balance-due check runs against the row this call locks, not a
    value the caller read earlier -- a second, stale-balance payment call
    cannot push amount_settled past total."""
    order, cashier = place(customer(), price="40.00", qty=1), member()
    record_ledger_payment(order, order.total, "cash", cashier)
    set_committed_value(order, "amount_settled", D("0.00"))  # stale: "nothing paid yet"
    with pytest.raises(LedgerPaymentError):
        record_ledger_payment(order, order.total, "cash", cashier)
    assert order.amount_settled == order.total
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 1


def test_money_is_decimal_not_float(db):
    order, cashier = place(customer(), price="0.10", qty=3), member()
    entry = record_ledger_payment(order, order.total, "cash", cashier)
    assert isinstance(entry.amount, Decimal) and entry.amount == order.total


# --- M7.5.3.7: _clear_ledger_self_references must not force-activate the
# shared app/db fixture for a test that never asked for it -------------------


def test_clear_ledger_self_references_does_not_activate_app_for_a_test_without_it():
    """A test that requests neither `app` nor `db` (the shape every
    tests/test_payment_ledger_migration.py test takes, building its own
    independent application instead) must never have the shared app/db
    fixture silently activated by the autouse
    _clear_ledger_self_references fixture. If it were, this test would be
    running inside a pushed Flask application context -- it is not."""
    from flask import has_app_context
    assert not has_app_context()


def test_clear_ledger_self_references_still_cleans_up_when_db_is_used(db):
    """Positive case, for contrast with the test above: a test that *does*
    use the standard db fixture still gets its refund self-references
    cleared before drop_all() -- unchanged behavior, just no longer
    unconditionally activated. (The real proof is that this test, and
    every other ledger-refund test in the suite, tears down cleanly
    without the SQLite 'FOREIGN KEY constraint failed' DROP TABLE error
    documented on the fixture itself; this just makes the data-level
    precondition for that explicit.)"""
    order, cashier = place(customer(), price="20.00", qty=1), member()
    payment = record_ledger_payment(order, order.total, "cash", cashier)
    refund_ledger_payment(payment, "1.00", "test", cashier)
    assert _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.REFUND
    ).count() == 1
