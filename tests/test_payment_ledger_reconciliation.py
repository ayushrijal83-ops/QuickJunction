"""Milestone 7.5.4: scripts/reconcile_payment_ledger.py -- backfills a
payment ledger entry (and refund entry, where applicable) for an order paid
through the legacy app/services/payments.py route after the M7.5.1
migration's own one-time backfill ran but before the M7.5.3 cutover deployed.
See that script's module docstring for the full safety model; these tests
exercise scan()/apply_reconciliation() directly against the standard db
fixture, the same way test_payment_ledger_service.py exercises the service
layer -- no subprocess involved."""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.extensions import db as _db
from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.services.payment_ledger import record_ledger_payment
from scripts.reconcile_payment_ledger import (
    apply_reconciliation,
    build_dry_run_report,
    scan,
)
from tests.conftest import make_legacy_payment, refund_legacy_payment
from tests.test_payments import member
from tests.test_restaurant_ops import customer, place

D = Decimal


def _legacy_paid_order(price="100.00", qty=1, refund_amount=None, reason="damaged", buyer=None, cashier=None):
    """A payments row in exactly the state M7.5.3's reconciliation gap
    describes: amount_settled still 0, no ledger entry at all -- never
    touched by the ledger service. Built directly via the make_legacy_payment
    fixture (M7.5.4.7 retired the app/services/payments.py route that used
    to produce this shape for real); the resulting row is identical either
    way.

    ``buyer``/``cashier`` let a test reuse one pair of accounts across
    several orders -- ``customer()``/``member()`` each insert a user with a
    fixed default username, so calling either twice in one test without an
    override collides on that username."""
    buyer = buyer or customer()
    cashier = cashier or member()
    order = place(buyer, price=price, qty=qty)
    payment = make_legacy_payment(order, cashier)
    if refund_amount is not None:
        refund_legacy_payment(payment, refund_amount, reason)
    _db.session.refresh(order)
    assert order.amount_settled == D("0.00")
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 0
    return order, payment


# --- normal historical cases -------------------------------------------------------------


def test_unrefunded_legacy_payment_is_eligible_and_reconciles_cleanly(db):
    order, payment = _legacy_paid_order(price="40.00")

    result = scan()
    assert order.id in [p.order_id for p in result.clean]
    assert result.already_reconciled_count == 0

    report = apply_reconciliation(result)
    reconciled = report.by_action("reconciled")
    assert len(reconciled) == 1 and reconciled[0].order_id == order.id

    entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT).one()
    assert entry.amount == payment.amount
    assert entry.method == payment.method
    assert entry.recorded_by_id == payment.recorded_by_id
    assert entry.created_at == payment.captured_at
    assert entry.refunded_amount == D("0.00")
    assert entry.corrected_amount == D("0.00")
    assert "reconcile_payment_ledger.py" in entry.reason
    assert _db.session.get(Order, order.id).amount_settled == payment.amount
    assert _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.REFUND).count() == 0


def test_partially_refunded_legacy_payment_creates_payment_and_refund_entries(db):
    order, payment = _legacy_paid_order(price="80.00", refund_amount="20.00", reason="too spicy")

    result = scan()
    report = apply_reconciliation(result)
    assert len(report.by_action("reconciled")) == 1

    payment_entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT).one()
    refund_entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.REFUND).one()

    assert payment_entry.refunded_amount == D("20.00")
    assert refund_entry.amount == D("-20.00")
    assert refund_entry.reversed_entry_id == payment_entry.id
    assert refund_entry.reason == "too spicy"
    assert refund_entry.recorded_by_id is None  # legacy refunds never name an actor
    assert refund_entry.created_at == payment.refunded_at
    # payment.amount is the order's real total (price plus tax/pricing rules,
    # M12) -- not the nominal "price" string passed to place().
    assert _db.session.get(Order, order.id).amount_settled == payment.amount - D("20.00")


def test_fully_refunded_legacy_payment_settles_to_zero_but_is_not_unpaid(db):
    order, cashier = place(customer(), price="50.00", qty=1), member()
    payment = make_legacy_payment(order, cashier)
    refund_legacy_payment(payment, payment.amount, "cancelled by kitchen")  # the whole total, whatever tax made it
    _db.session.refresh(order)

    report = apply_reconciliation(scan())
    assert len(report.by_action("reconciled")) == 1
    assert _db.session.get(Order, order.id).amount_settled == D("0.00")
    # Distinguishing mark vs. a never-paid order: a payment entry exists.
    assert _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT).count() == 1


# --- idempotency --------------------------------------------------------------------------


def test_rerunning_apply_is_a_clean_noop_the_second_time(db):
    order, payment = _legacy_paid_order(price="30.00")

    first = apply_reconciliation(scan())
    assert len(first.by_action("reconciled")) == 1

    second_scan = scan()
    assert second_scan.clean == []
    assert second_scan.already_reconciled_count == 1

    second = apply_reconciliation(second_scan)
    assert second.by_action("reconciled") == []
    assert second.by_action("flagged") == []
    assert second.by_action("failed") == []

    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 1
    assert _db.session.get(Order, order.id).amount_settled == payment.amount


# --- already reconciled (legacy + ledger both present -- the common, fine case) -----------


def test_order_with_both_legacy_payment_and_ledger_entry_is_not_eligible(db):
    """The ordinary state of every order that predates M7.5.1: both exist
    together and nothing needs to happen."""
    order, cashier = place(customer(), price="25.00", qty=1), member()
    make_legacy_payment(order, cashier)
    # Simulates what the M7.5.1 migration's own backfill would have done for
    # a pre-existing order: both a payments row and a matching ledger entry.
    _db.session.add(PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT, amount=order.total, method="cash",
        refunded_amount=D("0.00"), corrected_amount=D("0.00"), recorded_by_id=cashier.id,
    ))
    _db.session.execute(
        Order.__table__.update().where(Order.id == order.id).values(amount_settled=order.total)
    )
    _db.session.commit()

    result = scan()
    assert order.id not in [p.order_id for p in result.clean]
    assert order.id not in [p.order_id for p, _ in result.flagged]
    assert result.already_reconciled_count == 1

    report = apply_reconciliation(result)
    assert report.by_action("reconciled") == []
    # Untouched: still exactly one payment entry, not duplicated.
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 1


# --- flagged / inconsistent records --------------------------------------------------------


def test_nonzero_amount_settled_with_no_ledger_entry_is_flagged_not_overwritten(db):
    order, payment = _legacy_paid_order(price="60.00")
    # Simulates a precondition violation that should be structurally
    # impossible today (see module docstring) -- something set
    # amount_settled without ever creating a ledger entry.
    _db.session.execute(
        Order.__table__.update().where(Order.id == order.id).values(amount_settled=D("12.34"))
    )
    _db.session.commit()

    result = scan()
    assert order.id not in [p.order_id for p in result.clean]
    flagged_order_ids = [p.order_id for p, _ in result.flagged]
    assert order.id in flagged_order_ids
    reason = dict((p.order_id, r) for p, r in result.flagged)[order.id]
    assert "amount_settled" in reason

    report = apply_reconciliation(result)
    assert report.by_action("reconciled") == []
    assert _db.session.get(Order, order.id).amount_settled == D("12.34")  # untouched
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 0


def test_invalid_legacy_refunded_amount_is_flagged(db):
    """ck_payments_refund_within_amount makes an out-of-range refunded_amount
    unreachable through any service or even a direct UPDATE -- SQLite and
    MySQL both enforce it unconditionally. _ineligibility_reason's check is
    defensive against a future schema change or a row from outside this
    application, so it is exercised directly against an unpersisted,
    deliberately invalid object rather than a real database row."""
    from scripts.reconcile_payment_ledger import _ineligibility_reason

    order, payment = _legacy_paid_order(price="40.00")
    invalid = Payment(order_id=order.id, method=payment.method, amount=D("40.00"),
                       refunded_amount=D("999.00"), recorded_by_id=payment.recorded_by_id)
    reason = _ineligibility_reason(invalid)
    assert reason is not None and "out of range" in reason

    missing_actor = Payment(order_id=order.id, method=payment.method, amount=D("40.00"),
                             refunded_amount=D("0.00"), recorded_by_id=None)
    reason2 = _ineligibility_reason(missing_actor)
    assert reason2 is not None and "recorded_by_id" in reason2

    assert _ineligibility_reason(payment) is None


# --- mixed legacy/new activity (the critical financial-safety case) -----------------------


def test_apply_never_overwrites_a_genuine_ledger_payment_recorded_after_the_scan(db):
    """The scenario the migration's own blanket settlement UPDATE could not
    safely handle: by the time apply runs, the order already has real,
    independent ledger activity. Must be left completely alone."""
    order, payment = _legacy_paid_order(price="70.00")
    result = scan()
    assert order.id in [p.order_id for p in result.clean]

    # A genuine new payment lands on the very order the stale scan still
    # thinks is untouched -- e.g. a race, or the UI guard being bypassed.
    cashier = member(name="cashier2")
    new_entry = record_ledger_payment(order, "30.00", "card", cashier)
    _db.session.refresh(order)
    assert order.amount_settled == D("30.00")

    report = apply_reconciliation(result)
    flagged = report.by_action("flagged")
    assert any(o.order_id == order.id for o in flagged)
    assert report.by_action("reconciled") == []

    # The genuine payment is completely intact -- not clobbered, not doubled.
    assert _db.session.get(Order, order.id).amount_settled == D("30.00")
    entries = _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).all()
    assert len(entries) == 1 and entries[0].id == new_entry.id


# --- stale-read regression (M7.5.4.4) ------------------------------------------------------
#
# M7.5.4.3's independent review found that _apply_one re-verified the *order*
# (existing ledger entry? amount_settled still 0?) under its lock, but kept
# using the legacy Payment object this run's own earlier scan() had already
# loaded -- never re-reading it after the lock was taken. These tests pin
# the fix: _apply_one must re-fetch and lock the payment row itself, and use
# only that fresh copy for the ledger entries and the settlement amount.
#
# A real second thread cannot share this test's private in-memory SQLite
# database the way tests/test_mysql_concurrency.py's threads share a real
# MySQL connection pool, so "another writer changed this row after the scan"
# is simulated deterministically instead: a Core-level UPDATE with
# synchronize_session=False -- the same technique every conditional UPDATE
# in this codebase already uses -- changes the row without that strategy
# touching, expiring, or refreshing any ORM object already loaded in this
# session. From the session's point of view this is indistinguishable from
# a different connection having committed the same change: the already-
# loaded `scanned_payment` object stays exactly as stale either way. No
# sleep, no thread, no timing dependency.


def test_apply_uses_the_locked_payment_row_not_the_scan_time_copy(db):
    """Regression for the M7.5.4.3 review finding: a refund lands on the
    exact legacy payment row being backfilled, after scan() has already
    loaded it, before apply_reconciliation() processes it. The ledger entry
    and amount_settled this run writes must reflect the refund -- never the
    pre-refund amount the scan happened to see."""
    order, payment = _legacy_paid_order(price="50.00")

    result = scan()
    scanned_payment = next(p for p in result.clean if p.order_id == order.id)
    assert scanned_payment.refunded_amount == D("0.00")  # what the scan saw

    # The simulated concurrent write: committed as far as the database row
    # is concerned, but -- by construction (synchronize_session=False) --
    # never synchronized into `scanned_payment`, the object apply_reconciliation
    # is about to reuse from `result`.
    _db.session.execute(
        sa.update(Payment).where(Payment.id == payment.id)
        .values(refunded_amount=D("15.00"), refunded_at=sa.func.now(), refund_reason="late (simulated race)")
        .execution_options(synchronize_session=False)
    )
    assert scanned_payment.refunded_amount == D("0.00")  # still stale in this session's identity map

    report = apply_reconciliation(result)
    assert len(report.by_action("reconciled")) == 1

    entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT).one()
    refund_entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.REFUND).one()

    # Reflects the row under the lock, not the scan-time copy.
    assert entry.refunded_amount == D("15.00")
    assert refund_entry.amount == D("-15.00")
    assert refund_entry.reason == "late (simulated race)"
    assert _db.session.get(Order, order.id).amount_settled == payment.amount - D("15.00")


def test_payment_row_disappearing_after_the_order_lock_is_flagged_not_crashed(db, monkeypatch):
    """Defensive path: the legacy payment row is gone by the time
    _lock_payment_for_update runs, even though it still existed moments
    earlier for the order_id lookup. Forced via monkeypatch (there is no
    code path in this application that deletes a payments row, so this
    cannot be reproduced any other way) -- must flag and leave the order
    completely untouched, never raise an unhandled exception."""
    import scripts.reconcile_payment_ledger as reconcile_module

    order, payment = _legacy_paid_order(price="35.00")
    payment_id = payment.id
    real_lock_order = reconcile_module._lock_order_for_update

    def lock_order_then_delete_payment(order_id):
        locked = real_lock_order(order_id)
        _db.session.execute(
            sa.delete(Payment).where(Payment.id == payment_id).execution_options(synchronize_session=False)
        )
        return locked

    monkeypatch.setattr(reconcile_module, "_lock_order_for_update", lock_order_then_delete_payment)

    outcome = reconcile_module._apply_one(payment_id)
    assert outcome.action == "flagged"
    assert "disappeared" in outcome.detail

    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 0
    assert _db.session.get(Order, order.id).amount_settled == D("0.00")


# --- failure handling / transaction rollback ------------------------------------------------


def test_one_failing_order_is_rolled_back_and_other_orders_still_reconcile(db, monkeypatch):
    buyer, cashier = customer(), member()
    good_order, _ = _legacy_paid_order(price="10.00", buyer=buyer, cashier=cashier)
    bad_order, _ = _legacy_paid_order(price="20.00", buyer=buyer, cashier=cashier)

    import scripts.reconcile_payment_ledger as reconcile_module

    real_lock = reconcile_module._lock_order_for_update
    call_count = {"n": 0}

    def flaky_lock(order_id):
        call_count["n"] += 1
        if order_id == bad_order.id:
            raise RuntimeError("simulated lock failure")
        return real_lock(order_id)

    monkeypatch.setattr(reconcile_module, "_lock_order_for_update", flaky_lock)

    result = scan()
    report = apply_reconciliation(result)

    reconciled_ids = {o.order_id for o in report.by_action("reconciled")}
    failed_ids = {o.order_id for o in report.by_action("failed")}
    assert good_order.id in reconciled_ids
    assert bad_order.id in failed_ids

    # The failed order was rolled back cleanly: no half-written ledger row,
    # amount_settled untouched.
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=bad_order.id).count() == 0
    assert _db.session.get(Order, bad_order.id).amount_settled == D("0.00")
    # The good order is unaffected by the other order's failure.
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=good_order.id).count() == 1


def test_dry_run_never_writes_anything(db):
    order, _ = _legacy_paid_order(price="15.00")
    result = scan()
    report = build_dry_run_report(result)
    assert len(report.by_action("eligible")) == 1

    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 0
    assert _db.session.get(Order, order.id).amount_settled == D("0.00")


# --- opt-in MySQL verification --------------------------------------------------------------


@pytest.fixture
def mysql_only(app):
    from app.extensions import db as mysql_db
    if mysql_db.engine.dialect.name != "mysql":
        pytest.skip("live MySQL verification needs TEST_DATABASE_URL=mysql+pymysql://...")


def test_mysql_reconciliation_matches_sqlite_behavior(mysql_only, db):
    """Opt-in: runs for real only when TEST_DATABASE_URL points at MySQL
    (the mysql_only fixture above skips it otherwise), proving the
    row-locking path (SELECT ... FOR UPDATE) that SQLite accepts but does
    not actually enforce behaves the same against InnoDB."""
    order, payment = _legacy_paid_order(price="45.00", refund_amount="5.00", reason="late")

    report = apply_reconciliation(scan())
    assert len(report.by_action("reconciled")) == 1
    assert _db.session.get(Order, order.id).amount_settled == payment.amount - D("5.00")
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 2

    # Idempotent against MySQL too.
    second = apply_reconciliation(scan())
    assert second.by_action("reconciled") == []
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 2
