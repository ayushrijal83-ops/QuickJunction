"""Milestone 7.5.1: the payment_ledger_entries model and its constraints,
and orders.amount_settled. Schema only -- nothing in the service layer
writes here yet (see tests/test_payment_ledger_migration.py for the
migration's backfill and downgrade safeguards)."""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.extensions import db as _db
from app.models.order import Order
from app.models.payment import Payment, PaymentMethod
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.services.payments import record_payment
from tests.conftest import rejected_by_check_constraint
from tests.test_restaurant_ops import customer, place
from tests.test_payments import member

D = Decimal


@pytest.fixture(autouse=True)
def _clear_ledger_self_references(app):
    """SQLite enforces ON DELETE RESTRICT even while dropping the whole
    self-referencing table (a real sqlite3 limitation -- 'FOREIGN KEY
    constraint failed' straight out of DROP TABLE; InnoDB has no such issue
    dropping a table regardless of its own FK content). Break the
    self-reference before the `app` fixture's drop_all() runs; this changes
    nothing about the RESTRICT the schema itself enforces at runtime."""
    yield
    _db.session.execute(sa.text("DELETE FROM payment_ledger_entries WHERE reversed_entry_id IS NOT NULL"))
    _db.session.execute(sa.text("DELETE FROM payment_ledger_entries"))
    _db.session.commit()


def _payment_entry(order: Order, actor, *, amount="100.00", method=PaymentMethod.CASH,
                    refunded_amount="0.00", corrected_amount="0.00") -> PaymentLedgerEntry:
    entry = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT, amount=D(amount), method=method,
        refunded_amount=D(refunded_amount), corrected_amount=D(corrected_amount), recorded_by_id=actor.id,
    )
    _db.session.add(entry)
    _db.session.commit()
    return entry


# --- valid inserts, one per entry_type ----------------------------------------------


def test_payment_entry_valid_insert(db):
    order, cashier = place(customer()), member()
    entry = _payment_entry(order, cashier, amount="226.00")
    assert entry.id is not None
    assert (entry.entry_type, entry.amount, entry.method) == (LedgerEntryType.PAYMENT, D("226.00"), PaymentMethod.CASH)
    assert (entry.refunded_amount, entry.corrected_amount) == (D("0.00"), D("0.00"))
    assert entry.reversed_entry_id is None


def test_refund_entry_valid_insert(db):
    order, cashier = place(customer()), member()
    payment = _payment_entry(order, cashier, amount="100.00", refunded_amount="40.00")
    refund = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.REFUND, amount=D("-40.00"),
        reversed_entry_id=payment.id, reason="cold fries", recorded_by_id=cashier.id,
    )
    _db.session.add(refund)
    _db.session.commit()
    assert refund.amount == D("-40.00")
    assert refund.method is None and refund.refunded_amount is None and refund.corrected_amount is None


def test_adjustment_entry_valid_insert(db):
    order, cashier = place(customer()), member()
    adjustment = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.ADJUSTMENT, amount=D("5.00"), recorded_by_id=cashier.id,
    )
    _db.session.add(adjustment)
    _db.session.commit()
    assert adjustment.reversed_entry_id is None and adjustment.method is None


def test_correction_entry_valid_insert(db):
    order, cashier = place(customer()), member()
    payment = _payment_entry(order, cashier, amount="100.00", corrected_amount="-10.00")
    correction = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.CORRECTION, amount=D("-10.00"),
        reversed_entry_id=payment.id, reason="price typo", recorded_by_id=cashier.id,
    )
    _db.session.add(correction)
    _db.session.commit()
    assert correction.amount == D("-10.00")


# --- entry_type allow-list ----------------------------------------------------------


def test_entry_type_allow_list_enforced(db):
    order = place(customer())
    with rejected_by_check_constraint():
        _db.session.execute(sa.text(
            "INSERT INTO payment_ledger_entries (order_id, entry_type, amount) VALUES (:o, 'tip', 5.00)"
        ), {"o": order.id})
        _db.session.flush()
    _db.session.rollback()


# --- amount sign per entry_type ------------------------------------------------------


@pytest.mark.parametrize("amount", ["0.00", "-1.00"])
def test_payment_amount_must_be_positive(db, amount):
    order, cashier = place(customer()), member()
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.PAYMENT, amount=D(amount), method=PaymentMethod.CASH,
            refunded_amount=D("0.00"), corrected_amount=D("0.00"), recorded_by_id=cashier.id,
        ))
        _db.session.flush()
    _db.session.rollback()


@pytest.mark.parametrize("amount", ["0.00", "1.00"])
def test_refund_amount_must_be_negative(db, amount):
    order, cashier = place(customer()), member()
    payment = _payment_entry(order, cashier, amount="100.00")
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.REFUND, amount=D(amount), reversed_entry_id=payment.id,
        ))
        _db.session.flush()
    _db.session.rollback()


@pytest.mark.parametrize("entry_type", [LedgerEntryType.ADJUSTMENT, LedgerEntryType.CORRECTION])
def test_adjustment_and_correction_amount_must_be_nonzero(db, entry_type):
    order, cashier = place(customer()), member()
    reversed_id = _payment_entry(order, cashier).id if entry_type == LedgerEntryType.CORRECTION else None
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=entry_type, amount=D("0.00"), reversed_entry_id=reversed_id,
        ))
        _db.session.flush()
    _db.session.rollback()


# --- method presence -------------------------------------------------------------------


def test_payment_requires_method(db):
    order, cashier = place(customer()), member()
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.PAYMENT, amount=D("10.00"), method=None,
            refunded_amount=D("0.00"), corrected_amount=D("0.00"), recorded_by_id=cashier.id,
        ))
        _db.session.flush()
    _db.session.rollback()


@pytest.mark.parametrize("entry_type", [LedgerEntryType.ADJUSTMENT, LedgerEntryType.REFUND, LedgerEntryType.CORRECTION])
def test_non_payment_entries_must_have_null_method(db, entry_type):
    order, cashier = place(customer()), member()
    reversed_id = _payment_entry(order, cashier).id if entry_type != LedgerEntryType.ADJUSTMENT else None
    amount = D("-5.00") if entry_type != LedgerEntryType.ADJUSTMENT else D("5.00")
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=entry_type, amount=amount, method=PaymentMethod.CASH,
            reversed_entry_id=reversed_id,
        ))
        _db.session.flush()
    _db.session.rollback()


# --- reversed_entry_id presence -------------------------------------------------------


@pytest.mark.parametrize("entry_type,amount", [(LedgerEntryType.REFUND, "-5.00"), (LedgerEntryType.CORRECTION, "-5.00")])
def test_refund_and_correction_require_reversed_entry_id(db, entry_type, amount):
    order = place(customer())
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(order_id=order.id, entry_type=entry_type, amount=D(amount)))
        _db.session.flush()
    _db.session.rollback()


def test_payment_must_not_have_reversed_entry_id(db):
    order, cashier = place(customer()), member()
    other = _payment_entry(order, cashier)
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.PAYMENT, amount=D("10.00"), method=PaymentMethod.CASH,
            refunded_amount=D("0.00"), corrected_amount=D("0.00"), recorded_by_id=cashier.id,
            reversed_entry_id=other.id,
        ))
        _db.session.flush()
    _db.session.rollback()


def test_adjustment_must_not_have_reversed_entry_id(db):
    order, cashier = place(customer()), member()
    payment = _payment_entry(order, cashier)
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.ADJUSTMENT, amount=D("5.00"), reversed_entry_id=payment.id,
        ))
        _db.session.flush()
    _db.session.rollback()


# --- refunded_amount / corrected_amount only on 'payment' -----------------------------


@pytest.mark.parametrize("field", ["refunded_amount", "corrected_amount"])
def test_only_payment_entries_may_have_projection_fields(db, field):
    order, cashier = place(customer()), member()
    payment = _payment_entry(order, cashier)
    kwargs = {field: D("1.00")}
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.REFUND, amount=D("-1.00"),
            reversed_entry_id=payment.id, **kwargs,
        ))
        _db.session.flush()
    _db.session.rollback()


def test_payment_entry_requires_projection_fields_populated(db):
    """The CHECK requires refunded_amount/corrected_amount to be set (not
    NULL) on a 'payment' row, not merely absent on every other row."""
    order, cashier = place(customer()), member()
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.PAYMENT, amount=D("10.00"), method=PaymentMethod.CASH,
            refunded_amount=None, corrected_amount=D("0.00"), recorded_by_id=cashier.id,
        ))
        _db.session.flush()
    _db.session.rollback()


# --- effective amount cannot go negative (== refunds cannot exceed it) ----------------


def test_payment_effective_amount_cannot_become_negative(db):
    order, cashier = place(customer()), member()
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.PAYMENT, amount=D("100.00"), method=PaymentMethod.CASH,
            refunded_amount=D("100.01"), corrected_amount=D("0.00"), recorded_by_id=cashier.id,
        ))
        _db.session.flush()
    _db.session.rollback()


def test_correction_can_rescue_an_otherwise_negative_effective_amount(db):
    """A positive correction widens how much can be refunded -- the two
    bullet points in the brief ('effective amount >= 0' and 'refunds cannot
    exceed the effective amount') are the same constraint, not two."""
    order, cashier = place(customer()), member()
    entry = _payment_entry(order, cashier, amount="100.00", refunded_amount="100.00", corrected_amount="20.00")
    assert entry.id is not None


# --- foreign keys ----------------------------------------------------------------------


def test_order_id_foreign_key_requires_existing_order(db):
    with pytest.raises(sa.exc.IntegrityError):
        _db.session.add(PaymentLedgerEntry(
            order_id=999999, entry_type=LedgerEntryType.ADJUSTMENT, amount=D("1.00"),
        ))
        _db.session.flush()
    _db.session.rollback()


def test_reversed_entry_id_foreign_key_requires_existing_entry(db):
    order = place(customer())
    with pytest.raises(sa.exc.IntegrityError):
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.REFUND, amount=D("-1.00"), reversed_entry_id=999999,
        ))
        _db.session.flush()
    _db.session.rollback()


def test_reversed_entry_id_fk_does_not_itself_require_a_payment_row(db):
    """Documented limitation (module docstring): the FK only guarantees the
    referenced row exists, not that it is a 'payment' entry. A correction
    pointing at another correction is accepted at the database level."""
    order, cashier = place(customer()), member()
    first = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.ADJUSTMENT, amount=D("5.00"), recorded_by_id=cashier.id,
    )
    _db.session.add(first)
    _db.session.commit()
    second = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.CORRECTION, amount=D("-1.00"), reversed_entry_id=first.id,
        recorded_by_id=cashier.id,
    )
    _db.session.add(second)
    _db.session.commit()
    assert second.reversed_entry_id == first.id


def test_order_id_restrict_blocks_deleting_an_order_with_ledger_history(db):
    order, cashier = place(customer()), member()
    _payment_entry(order, cashier)
    with pytest.raises(sa.exc.IntegrityError):
        _db.session.execute(sa.text("DELETE FROM orders WHERE id = :i"), {"i": order.id})
        _db.session.flush()
    _db.session.rollback()


def test_recorded_by_id_nullable_for_refund_only(db):
    """Historical refunds backfilled from the legacy schema cannot always
    name an actor (see the migration) -- 'refund' is the only entry_type
    ck_payment_ledger_entries_recorded_by_presence lets go without one."""
    order, cashier = place(customer()), member()
    payment = _payment_entry(order, cashier)
    refund = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.REFUND, amount=D("-5.00"),
        reversed_entry_id=payment.id, recorded_by_id=None,
    )
    _db.session.add(refund)
    _db.session.commit()
    assert refund.recorded_by_id is None


@pytest.mark.parametrize("entry_type", [LedgerEntryType.PAYMENT, LedgerEntryType.ADJUSTMENT])
def test_recorded_by_id_required_for_payment_and_adjustment(db, entry_type):
    order = place(customer())
    kwargs = {"method": PaymentMethod.CASH, "refunded_amount": D("0.00"), "corrected_amount": D("0.00")} \
        if entry_type == LedgerEntryType.PAYMENT else {}
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=entry_type, amount=D("5.00"), recorded_by_id=None, **kwargs,
        ))
        _db.session.flush()
    _db.session.rollback()


def test_recorded_by_id_required_for_correction(db):
    order, cashier = place(customer()), member()
    payment = _payment_entry(order, cashier)
    with rejected_by_check_constraint():
        _db.session.add(PaymentLedgerEntry(
            order_id=order.id, entry_type=LedgerEntryType.CORRECTION, amount=D("-5.00"),
            reversed_entry_id=payment.id, recorded_by_id=None,
        ))
        _db.session.flush()
    _db.session.rollback()


# --- orders.amount_settled ------------------------------------------------------------


def test_amount_settled_defaults_to_zero(db):
    order = place(customer(), price="100.00", qty=1)
    assert order.amount_settled == D("0.00")


def test_amount_settled_cannot_be_negative(db):
    order = place(customer())
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("UPDATE orders SET amount_settled = -1 WHERE id = :i"), {"i": order.id})
        _db.session.flush()
    _db.session.rollback()


def test_amount_settled_cannot_exceed_total(db):
    order = place(customer())
    with rejected_by_check_constraint():
        _db.session.execute(
            sa.text("UPDATE orders SET amount_settled = total + 1 WHERE id = :i"), {"i": order.id}
        )
        _db.session.flush()
    _db.session.rollback()


def test_amount_settled_can_be_set_up_to_total(db):
    order = place(customer())
    _db.session.execute(sa.text("UPDATE orders SET amount_settled = total WHERE id = :i"), {"i": order.id})
    _db.session.commit()
    _db.session.refresh(order)
    assert order.amount_settled == order.total


def test_money_is_decimal_not_float(db):
    order, cashier = place(customer()), member()
    entry = _payment_entry(order, cashier, amount="0.34")
    assert isinstance(entry.amount, Decimal) and entry.amount == D("0.34")
