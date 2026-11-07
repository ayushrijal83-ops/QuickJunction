"""Milestone 7.5.4.9: scripts/audit_payment_ledger.py -- the permanent,
read-only payment-ledger integrity audit. See that script's own module
docstring for the full rule set (A1/A2/A3/A4/B2/C1), the "awaiting
reconciliation" informational classification, and the read-only design
(why SQLite and MySQL use different read strategies -- verified
empirically there, exercised here).

Exercises run_audit() directly against the standard db fixture for every
rule and every "this must stay clean" edge case; a small, separate section
exercises main() (the CLI) for exit codes and the production-target gate,
using the same create_app()-monkeypatch technique
tests/test_reconcile_cli_safety.py established for exactly this reason
(config.BaseConfig binds DATABASE_URL at class-definition time, which has
already happened once conftest.py's own imports ran)."""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest
import sqlalchemy as sa

import app as app_pkg
import scripts.audit_payment_ledger as audit_module
from app.extensions import db as _db
from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.services.payment_ledger import record_ledger_payment, refund_ledger_payment
from scripts.audit_payment_ledger import run_audit
from tests.conftest import make_legacy_payment
from tests.test_payments import member
from tests.test_restaurant_ops import customer, place

D = Decimal
_seq = itertools.count()


def _paid_order(price="100.00"):
    """One order with a fresh, uniquely-named customer/cashier pair --
    every order in this file gets its own pair, since place()/
    make_legacy_payment() collide on their default usernames the moment a
    test seeds more than one."""
    n = next(_seq)
    return place(customer(username=f"diner{n}"), price=price, qty=1), member(name=f"cook{n}")


def _legacy_paid_order(price="100.00"):
    order, cashier = _paid_order(price)
    payment = make_legacy_payment(order, cashier)
    _db.session.refresh(order)
    return order, payment


def _reconcile_all() -> None:
    """Backfills a real ledger entry for every eligible legacy payment,
    using the actual reconciliation script's own write path -- the same
    shape reconciled production data would have. Imported locally so this
    file's own import graph stays obviously test-only."""
    from scripts.reconcile_payment_ledger import apply_reconciliation, scan

    apply_reconciliation(scan())


def _toggle_fk_enforcement(enabled: bool) -> None:
    """SQLite and MySQL enforce referential integrity differently enough
    that constructing a deliberately-dangling FK (the A2_MISSING_REVERSAL_TARGET
    defense-in-depth case) needs a dialect-specific, temporary override --
    restored immediately after, never left changed for any other test."""
    if _db.engine.dialect.name == "sqlite":
        _db.session.execute(sa.text(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}"))
    else:
        _db.session.execute(sa.text(f"SET FOREIGN_KEY_CHECKS={1 if enabled else 0}"))


# =============================================================================
# clean states -- must never produce a finding
# =============================================================================


def test_clean_ledger_native_order_is_fully_clean(app, db):
    order, cashier = _paid_order(price="40.00")
    record_ledger_payment(order, "19.00", "cash", cashier)
    record_ledger_payment(order, "21.00", "card", cashier)  # a second partial payment -- normal

    report = run_audit(_db.engine)

    assert report.is_clean
    assert report.awaiting_reconciliation_count == 0
    assert report.orders_checked >= 1


def test_clean_reconciled_history_is_fully_clean(app, db):
    order, payment = _legacy_paid_order(price="55.00")
    _reconcile_all()

    report = run_audit(_db.engine)

    assert report.is_clean
    assert report.awaiting_reconciliation_count == 0


def test_reconciled_order_with_subsequent_partial_ledger_refund_is_clean(app, db):
    """The key B2 edge case: once reconciled, a *further* refund recorded
    live through the ledger makes the ledger's refund total exceed the
    legacy payments.refunded_amount snapshot (which is frozen at 0 here,
    since this order was never refunded before reconciliation) -- expected,
    correct behavior, must never be flagged."""
    order, payment = _legacy_paid_order(price="80.00")
    _reconcile_all()

    entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT).one()
    actor = member(name=f"later_cashier{next(_seq)}")
    refund_ledger_payment(entry, "10.00", "customer complaint", actor)

    report = run_audit(_db.engine)

    assert report.is_clean
    assert report.by_rule("B2_LEGACY_REFUND_SHORTFALL") == []


def test_unreconciled_legacy_payment_is_informational_not_a_finding(app, db):
    order, payment = _legacy_paid_order(price="30.00")
    # Deliberately NOT reconciled.

    report = run_audit(_db.engine)

    assert report.is_clean
    assert report.awaiting_reconciliation_count == 1


# =============================================================================
# A1 -- payment entry refunded_amount projection vs. its own linked refunds
# =============================================================================


def test_a1_detects_refund_projection_mismatch(app, db):
    order, cashier = _paid_order(price="100.00")
    entry = record_ledger_payment(order, "100.00", "cash", cashier)
    refund_ledger_payment(entry, "20.00", "damaged", cashier)
    # Corrupt the projection directly -- bypassing the service layer, the
    # same technique tests/test_payment_ledger_reconciliation.py already
    # uses for its own "flagged, not overwritten" tests. 30 stays within
    # ck_payment_ledger_entries_effective_nonnegative (100 - 30 >= 0) so
    # this is a real, insertable mismatch, not a constraint violation.
    _db.session.execute(
        sa.update(PaymentLedgerEntry).where(PaymentLedgerEntry.id == entry.id)
        .values(refunded_amount=D("30.00")).execution_options(synchronize_session=False)
    )
    _db.session.commit()

    report = run_audit(_db.engine)

    findings = report.by_rule("A1_REFUND_PROJECTION_MISMATCH")
    assert len(findings) == 1
    assert findings[0].order_id == order.id
    assert "30.00" in findings[0].detail and "20.00" in findings[0].detail


def test_a1_detects_refund_projection_mismatch_the_other_direction(app, db):
    """A1's check is symmetric (!=, not >) -- a projection that
    UNDER-reports its actual linked refunds is just as real a mismatch as
    one that over-reports, and must be caught too. 5 stays within
    ck_payment_ledger_entries_effective_nonnegative (100 - 5 >= 0)."""
    order, cashier = _paid_order(price="100.00")
    entry = record_ledger_payment(order, "100.00", "cash", cashier)
    refund_ledger_payment(entry, "20.00", "damaged", cashier)
    _db.session.execute(
        sa.update(PaymentLedgerEntry).where(PaymentLedgerEntry.id == entry.id)
        .values(refunded_amount=D("5.00")).execution_options(synchronize_session=False)
    )
    _db.session.commit()

    report = run_audit(_db.engine)

    findings = report.by_rule("A1_REFUND_PROJECTION_MISMATCH")
    assert len(findings) == 1
    assert findings[0].order_id == order.id
    assert "5.00" in findings[0].detail and "20.00" in findings[0].detail


# =============================================================================
# A2/A3 -- reversal target missing, wrong type, or belonging to another order
# =============================================================================


def test_a2_detects_missing_reversal_target(app, db):
    """Defense in depth: the FK makes this impossible under normal
    operation (SQLite's PRAGMA foreign_keys=ON, enforced by this app's own
    "connect" event listener; InnoDB always enforces it), so this test
    constructs it directly, with FK enforcement deliberately toggled off
    just for this one insert."""
    order, cashier = _paid_order(price="50.00")

    _toggle_fk_enforcement(False)
    try:
        _db.session.execute(sa.text(
            "INSERT INTO payment_ledger_entries (order_id, entry_type, amount, reversed_entry_id, "
            "recorded_by_id, created_at) VALUES (:oid, 'refund', -5.00, 999999, :uid, "
            + ("CURRENT_TIMESTAMP" if _db.engine.dialect.name == "sqlite" else "NOW()") + ")"
        ), {"oid": order.id, "uid": cashier.id})
        _db.session.commit()
    finally:
        _toggle_fk_enforcement(True)

    report = run_audit(_db.engine)

    findings = report.by_rule("A2_MISSING_REVERSAL_TARGET")
    assert len(findings) == 1
    assert findings[0].order_id == order.id
    assert "999999" in findings[0].detail


def test_a2_detects_wrong_reversal_target_type(app, db):
    order, cashier = _paid_order(price="60.00")
    record_ledger_payment(order, "60.00", "cash", cashier)

    adjustment = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.ADJUSTMENT, amount=D("5.00"),
        recorded_by_id=cashier.id,
    )
    _db.session.add(adjustment)
    _db.session.flush()

    bogus_refund = PaymentLedgerEntry(
        order_id=order.id, entry_type=LedgerEntryType.REFUND, amount=D("-5.00"),
        reversed_entry_id=adjustment.id,
    )
    _db.session.add(bogus_refund)
    _db.session.commit()

    report = run_audit(_db.engine)

    findings = report.by_rule("A2_WRONG_REVERSAL_TARGET_TYPE")
    assert len(findings) == 1
    assert findings[0].order_id == order.id
    assert str(adjustment.id) in findings[0].detail
    assert "adjustment" in findings[0].detail


def test_a3_detects_cross_order_reversal(app, db):
    order_a, cashier_a = _paid_order(price="20.00")
    order_b, cashier_b = _paid_order(price="30.00")
    entry_a = record_ledger_payment(order_a, "20.00", "cash", cashier_a)
    record_ledger_payment(order_b, "30.00", "cash", cashier_b)

    # A 'refund' row that names order_b as its own order_id, but reverses a
    # payment entry that actually belongs to order_a.
    cross_order_refund = PaymentLedgerEntry(
        order_id=order_b.id, entry_type=LedgerEntryType.REFUND, amount=D("-5.00"),
        reversed_entry_id=entry_a.id,
    )
    _db.session.add(cross_order_refund)
    _db.session.commit()

    report = run_audit(_db.engine)

    findings = report.by_rule("A3_CROSS_ORDER_REVERSAL")
    assert len(findings) == 1
    assert findings[0].order_id == order_b.id
    assert str(order_a.id) in findings[0].detail


def test_a2_and_a3_both_fire_for_one_entry_that_violates_both(app, db):
    """A single corrupted reference can be both the wrong type AND belong
    to the wrong order at once (it reverses some *other* order's
    adjustment entry) -- both findings must be reported, not just
    whichever one the check happens to look at first."""
    order_a, cashier_a = _paid_order(price="40.00")
    order_b, cashier_b = _paid_order(price="50.00")
    record_ledger_payment(order_b, "50.00", "cash", cashier_b)  # unrelated, keeps order_b valid

    adjustment_on_a = PaymentLedgerEntry(
        order_id=order_a.id, entry_type=LedgerEntryType.ADJUSTMENT, amount=D("5.00"),
        recorded_by_id=cashier_a.id,
    )
    _db.session.add(adjustment_on_a)
    _db.session.flush()

    # A 'refund' entry on order_b reversing order_a's adjustment entry:
    # wrong type (adjustment, not payment) AND wrong order (a vs b) at once.
    both_wrong = PaymentLedgerEntry(
        order_id=order_b.id, entry_type=LedgerEntryType.REFUND, amount=D("-5.00"),
        reversed_entry_id=adjustment_on_a.id,
    )
    _db.session.add(both_wrong)
    _db.session.commit()

    report = run_audit(_db.engine)

    wrong_type = report.by_rule("A2_WRONG_REVERSAL_TARGET_TYPE")
    cross_order = report.by_rule("A3_CROSS_ORDER_REVERSAL")
    assert len(wrong_type) == 1 and wrong_type[0].order_id == order_b.id
    assert len(cross_order) == 1 and cross_order[0].order_id == order_b.id
    assert str(both_wrong.id) in wrong_type[0].detail
    assert str(both_wrong.id) in cross_order[0].detail


# =============================================================================
# A4/C1 -- Order.amount_settled vs. the ledger-derived net settlement
# =============================================================================


def test_a4_detects_settlement_mismatch_when_ledger_entries_exist(app, db):
    order, cashier = _paid_order(price="45.00")
    record_ledger_payment(order, "45.00", "cash", cashier)
    # Corrupt amount_settled directly, independent of the (correct) ledger.
    _db.session.execute(
        sa.update(Order).where(Order.id == order.id).values(amount_settled=D("12.34"))
        .execution_options(synchronize_session=False)
    )
    _db.session.commit()

    report = run_audit(_db.engine)

    findings = report.by_rule("A4_SETTLEMENT_MISMATCH")
    assert len(findings) == 1
    assert findings[0].order_id == order.id
    assert "12.34" in findings[0].detail and "45.00" in findings[0].detail
    assert report.by_rule("C1_SETTLED_WITH_NO_LEDGER_ENTRIES") == []


def test_c1_detects_settlement_with_zero_ledger_entries(app, db):
    order, _cashier = _paid_order(price="25.00")
    # No ledger entry at all -- set amount_settled directly, the way
    # nothing in the live application can (every live writer only ever
    # touches amount_settled in the same transaction as a ledger entry).
    _db.session.execute(
        sa.update(Order).where(Order.id == order.id).values(amount_settled=D("25.00"))
        .execution_options(synchronize_session=False)
    )
    _db.session.commit()

    report = run_audit(_db.engine)

    findings = report.by_rule("C1_SETTLED_WITH_NO_LEDGER_ENTRIES")
    assert len(findings) == 1
    assert findings[0].order_id == order.id
    # A4 must not *also* fire for the same order -- one label, not two.
    assert report.by_rule("A4_SETTLEMENT_MISMATCH") == []


# =============================================================================
# B2 -- legacy refunded_amount exceeding what the ledger ever recorded
# =============================================================================


def test_b2_detects_legacy_refund_shortfall(app, db):
    order, payment = _legacy_paid_order(price="90.00")
    _reconcile_all()
    # Simulates the pre-M7.5.4.7 gap: a legacy refund landing on the
    # payments row *after* reconciliation already ran, which the
    # (now-retired) legacy writer could do but nothing ever re-scanned for.
    _db.session.execute(
        sa.update(Payment).where(Payment.id == payment.id).values(refunded_amount=D("15.00"))
        .execution_options(synchronize_session=False)
    )
    _db.session.commit()

    report = run_audit(_db.engine)

    findings = report.by_rule("B2_LEGACY_REFUND_SHORTFALL")
    assert len(findings) == 1
    assert findings[0].order_id == order.id
    assert "15.00" in findings[0].detail and "0.00" in findings[0].detail


# =============================================================================
# read-only guarantees
# =============================================================================


def test_audit_writes_nothing_even_when_findings_exist(app, db):
    """A rich mix: clean ledger-native, clean reconciled, unreconciled
    legacy, and one genuine A4 violation -- proves run_audit() does not
    repair, touch, or otherwise write anything, including the very rows it
    is actively reporting as wrong."""
    clean_order, cashier = _paid_order(price="10.00")
    record_ledger_payment(clean_order, "10.00", "cash", cashier)

    legacy_order, _ = _legacy_paid_order(price="15.00")

    broken_order, broken_cashier = _paid_order(price="20.00")
    record_ledger_payment(broken_order, "20.00", "cash", broken_cashier)
    _db.session.execute(
        sa.update(Order).where(Order.id == broken_order.id).values(amount_settled=D("1.00"))
        .execution_options(synchronize_session=False)
    )
    _db.session.commit()

    def _snapshot():
        return {
            "orders": _db.session.execute(sa.text(
                "SELECT id, amount_settled FROM orders ORDER BY id")).all(),
            "payments": _db.session.execute(sa.text(
                "SELECT id, refunded_amount FROM payments ORDER BY id")).all(),
            "ledger": _db.session.execute(sa.text(
                "SELECT id, refunded_amount, amount FROM payment_ledger_entries ORDER BY id")).all(),
        }

    before = _snapshot()
    report = run_audit(_db.engine)
    after = _snapshot()

    assert report.by_rule("A4_SETTLEMENT_MISMATCH") != []  # the audit did see the problem
    assert before == after  # and did not touch a single row because of it
    assert not _db.session.new and not _db.session.dirty and not _db.session.deleted


def test_pending_ambient_session_changes_are_preserved_across_audit(app, db):
    """The SQLite read path (db.session, no_autoflush, never committed or
    rolled back) must leave a caller's own pending, uncommitted ORM
    mutation completely untouched -- not flushed to the database early,
    not silently discarded."""
    order, cashier = _paid_order(price="33.00")
    record_ledger_payment(order, "33.00", "cash", cashier)

    order.status = "confirmed"  # a real attribute mutation, never committed
    assert order in _db.session.dirty
    raw_status_before = _db.session.execute(
        sa.text("SELECT status FROM orders WHERE id = :id"), {"id": order.id}
    ).scalar()
    assert raw_status_before != "confirmed"  # not flushed yet

    run_audit(_db.engine)

    assert order in _db.session.dirty  # still pending -- not silently rolled back
    raw_status_during = _db.session.execute(
        sa.text("SELECT status FROM orders WHERE id = :id"), {"id": order.id}
    ).scalar()
    assert raw_status_during == raw_status_before  # still not flushed -- no autoflush happened

    _db.session.commit()
    assert _db.session.execute(
        sa.text("SELECT status FROM orders WHERE id = :id"), {"id": order.id}
    ).scalar() == "confirmed"


# =============================================================================
# isolation strategy selection (_read_only_executor itself)
# =============================================================================


def test_read_only_executor_refuses_an_unsupported_dialect():
    """A pure unit test of the fail-clearly fallback -- no real database
    needed, since the check happens before any connection is ever opened.
    Only sqlite and mysql have a verified read-only isolation story; a
    third dialect must raise loudly, never silently fall through to an
    unverified strategy."""
    class _FakeDialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _FakeDialect()

    with pytest.raises(audit_module.AuditIsolationError, match="postgresql"):
        with audit_module._read_only_executor(_FakeEngine()):
            pass  # pragma: no cover -- must never be reached


# =============================================================================
# CLI: exit codes and the production-target gate
# =============================================================================


def _fake_create_app(monkeypatch, application):
    monkeypatch.setattr(app_pkg, "create_app", lambda *a, **kw: application)


@pytest.fixture(autouse=True)
def _own_database_url_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused-placeholder")


def test_cli_exits_zero_when_clean(app, db, monkeypatch, capsys):
    order, cashier = _paid_order(price="15.00")
    record_ledger_payment(order, "15.00", "cash", cashier)
    _fake_create_app(monkeypatch, app)

    rc = audit_module.main(["--database-url", "sqlite:///irrelevant-app-is-faked.db"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "none -- the ledger is internally consistent" in out


def test_cli_exits_one_when_findings_exist(app, db, monkeypatch, capsys):
    order, cashier = _paid_order(price="15.00")
    record_ledger_payment(order, "15.00", "cash", cashier)
    _db.session.execute(
        sa.update(Order).where(Order.id == order.id).values(amount_settled=D("1.00"))
        .execution_options(synchronize_session=False)
    )
    _db.session.commit()
    _fake_create_app(monkeypatch, app)

    rc = audit_module.main(["--database-url", "sqlite:///irrelevant.db"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "A4_SETTLEMENT_MISMATCH" in out


def test_cli_non_test_mysql_target_is_refused_without_ever_building_an_app(monkeypatch, capsys):
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("create_app() must not run before the target gate decides")

    monkeypatch.setattr(app_pkg, "create_app", _must_not_be_called)

    rc = audit_module.main(["--database-url", "mysql+pymysql://user:pw@127.0.0.1:1/quick_junction"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "does not end in '_test'" in out
    assert "--allow-production-target" in out


def test_cli_production_override_clears_the_name_gate(app, db, monkeypatch, capsys):
    """Read-only, so there is no confirm phrase to also check here (unlike
    scripts/reconcile_payment_ledger.py) -- --allow-production-target is
    this tool's only gate, and clearing it is enough to proceed. The
    "mysql+pymysql://..." URL is only ever parsed for its database name
    here -- create_app() is faked to the in-memory SQLite test app, so no
    MySQL connection is attempted."""
    order, cashier = _paid_order(price="12.00")
    record_ledger_payment(order, "12.00", "cash", cashier)
    _fake_create_app(monkeypatch, app)

    rc = audit_module.main([
        "--database-url", "mysql+pymysql://user:pw@127.0.0.1/quick_junction",
        "--allow-production-target",
    ])

    assert rc == 0


def test_cli_malformed_database_url_is_rejected_safely(monkeypatch, capsys):
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("create_app() must not run for an unparsable --database-url")

    monkeypatch.setattr(app_pkg, "create_app", _must_not_be_called)

    rc = audit_module.main(["--database-url", "not a url at all"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAILED]" in out
    assert "not a url at all" not in out


def test_cli_sqlite_target_skips_the_production_name_gate_entirely(app, db, monkeypatch):
    order, cashier = _paid_order(price="8.00")
    record_ledger_payment(order, "8.00", "cash", cashier)
    _fake_create_app(monkeypatch, app)

    rc = audit_module.main(["--database-url", "sqlite:////tmp/quick_junction_production.db"])

    assert rc == 0


# =============================================================================
# opt-in MySQL verification
# =============================================================================


@pytest.fixture
def mysql_only(app):
    if _db.engine.dialect.name != "mysql":
        pytest.skip("live MySQL verification needs TEST_DATABASE_URL=mysql+pymysql://...")


def test_mysql_read_only_executor_establishes_repeatable_read(mysql_only, db):
    """Proves _read_only_executor's isolation-level override is actually
    invoked, not merely coincidentally matching InnoDB's own default (which
    is REPEATABLE READ out of the box -- a naive "assert the level is
    REPEATABLE READ afterward" check would still pass even with
    conn.execution_options(isolation_level="REPEATABLE READ") deleted
    entirely from scripts/audit_payment_ledger.py, on any unmodified MySQL
    server).

    This deliberately starts every connection from a real, different
    server-side state instead: a fresh, independent Engine (pointed at the
    same running MySQL server/database TEST_DATABASE_URL already names --
    no new server, no mocking of SQLAlchemy or of _read_only_executor
    itself) whose "connect" event forces each new physical connection to
    READ COMMITTED via a raw DBAPI cursor, entirely outside any code path
    _read_only_executor touches. Passing that engine to the real
    _read_only_executor and seeing REPEATABLE READ afterward can only be
    explained by the override actually running -- if it were removed, the
    connection would stay at READ COMMITTED and this test would fail."""
    import sqlalchemy as sa
    from sqlalchemy import event

    non_default_engine = sa.create_engine(_db.engine.url.render_as_string(hide_password=False))

    @event.listens_for(non_default_engine, "connect")
    def _start_every_new_connection_at_read_committed(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cursor.close()

    try:
        with audit_module._read_only_executor(non_default_engine) as executor:
            reported = executor.execute(sa.text("SELECT @@transaction_isolation")).scalar()
        assert (reported or "").upper().replace("-", " ") == "REPEATABLE READ"
    finally:
        non_default_engine.dispose()


def test_mysql_audit_matches_sqlite_behavior(mysql_only, db):
    """Opt-in: runs for real only against MySQL, proving the COALESCE/SUM/
    Numeric-typed raw queries (and the genuinely-separate-connection
    read-only path, never exercised at all under SQLite) behave the same
    as the SQLite path exercised by every test above."""
    order, payment = _legacy_paid_order(price="70.00")
    _reconcile_all()

    clean_report = run_audit(_db.engine)
    assert clean_report.is_clean

    entry = _db.session.query(PaymentLedgerEntry).filter_by(
        order_id=order.id, entry_type=LedgerEntryType.PAYMENT).one()
    _db.session.execute(
        sa.update(PaymentLedgerEntry).where(PaymentLedgerEntry.id == entry.id)
        .values(refunded_amount=D("5.00")).execution_options(synchronize_session=False)
    )
    _db.session.commit()

    broken_report = run_audit(_db.engine)
    assert len(broken_report.by_rule("A1_REFUND_PROJECTION_MISMATCH")) == 1
