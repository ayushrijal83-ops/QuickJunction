"""M7.5.5.1 -- real-schema backup -> restore -> payment-ledger audit drill.

tests/test_backup_restore.py::test_live_backup_and_restore_drill already
proves the backup/restore *mechanism* for real, against MySQL -- but only
for one small, custom table (``qj_backup_drill``) it creates for the
purpose, with no foreign keys and no CHECK constraints. It does not, and
was never meant to, prove that the *actual Quick Junction schema* --
``orders``/``payments``/``payment_ledger_entries``, with their real FKs and
CHECK constraints -- round-trips correctly, or that a restored copy's
financial data still passes ``scripts/audit_payment_ledger.py``'s
integrity checks. That is the one concrete gap the M7.5.5 read-only
inspection identified, and this file closes it.

This is an additive integration test only. It does not touch, mock, or
weaken ``scripts/backup_database.py``, ``scripts/restore_database.py``, or
``scripts/audit_payment_ledger.py`` -- every one of them runs for real,
exactly as an operator would invoke it.

Restore target design (a deliberate, explicit deviation from "restore into
a separate database", decided before writing this test): CI's MySQL
service grants the disposable ``qj_test`` account privileges scoped to
only the one database ``TEST_DATABASE_URL`` already names (the official
MySQL Docker image's ``MYSQL_USER``/``MYSQL_PASSWORD``/``MYSQL_DATABASE``
env vars do this, not a global ``CREATE DATABASE`` grant) -- confirmed by
grepping the whole repository for any existing precedent of creating a
second database with ordinary test credentials (none exists; the only
precedent, ``tests/test_privilege_separation.py``'s live drill, needs
``QJ_MYSQL_ADMIN_PASSWORD``, which CI never sets, and is always skipped in
CI as a result). A test built the same way here would likewise never
execute in this project's real CI, defeating the actual point of this
milestone. Instead, this test mirrors
``test_live_backup_and_restore_drill``'s own established pattern exactly:
wipe every table in the *same* disposable database (simulating total data
loss, the same way that test's own comment already describes it), then
restore into it. ``restore_database.py``'s own safety model does not care
whether a target is a "different" database by name -- only that it is
completely empty before anything is written to it (``ensure_target_is_empty``,
unconditional, not skippable) -- so this still exercises that exact same
safety gate for real.

Safe to rerun, by construction: the standard ``app``/``db`` fixture
(``tests/conftest.py``) already creates and drops the full schema around
every test that requests it, on any dialect. If this test's own body fails
between the wipe and the restore, the database is left with zero tables --
which that same fixture's own teardown (``drop_all()``, ``checkfirst=True``)
handles as a safe no-op, and the *next* test's setup (``create_all()``)
rebuilds everything from a clean slate regardless of what this test left
behind. The one already-accepted, pre-existing exposure this inherits
unchanged from ``restore_database.py`` itself is a process kill strictly
*during* the mysql restore subprocess, which that script's own docstring
for ``run_mysql_restore`` already documents as a known limitation -- not
something this test introduces or makes more likely."""

from __future__ import annotations

import itertools
from decimal import Decimal

import pymysql
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

import scripts.backup_database as bd
import scripts.restore_database as rd
from scripts._backup_crypto import generate_key_file
from app.extensions import db as _db
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.services.payment_ledger import record_ledger_payment, refund_ledger_payment
from scripts.audit_payment_ledger import run_audit
from tests.conftest import assert_disposable_test_database, make_legacy_payment, refund_legacy_payment, wipe_mysql_schema
from tests.test_payments import member
from tests.test_restaurant_ops import customer, place

D = Decimal
_seq = itertools.count()


def _paid_order(price="100.00", qty=1):
    n = next(_seq)
    return place(customer(username=f"drill_diner{n}"), price=price, qty=qty), member(name=f"drill_cook{n}")


@pytest.fixture
def mysql_test_url(app):
    """Matches tests/test_backup_restore.py's own fixture of the same name
    exactly (duplicated locally, not imported, per this test suite's
    established per-file convention -- see e.g. test_payment_ledger_audit.py's
    own local mysql_only fixture)."""
    import os

    uri = os.environ.get("TEST_DATABASE_URL", "")
    if not uri.startswith("mysql"):
        pytest.skip("backup/restore/audit drill needs TEST_DATABASE_URL=mysql+pymysql://...*_test")
    assert_disposable_test_database(uri)
    return uri


def test_real_schema_backup_restore_audit_drill(mysql_test_url, db, tmp_path):
    """Seeds realistic, internally consistent financial data -- through the
    real application paths for the ledger-native orders (checkout, the
    ledger service), and through direct model construction for the legacy,
    already-reconciled order (no reconciliation function is invoked here) --
    backs it up for real, destroys it, restores it for real, and asserts:
    the actual row-level data is back exactly, and the restored copy passes
    the real audit tool with zero integrity findings."""
    # --- seed: a fully-paid ledger-native order, with a partial refund ------
    full_order, full_cashier = _paid_order(price="40.00", qty=1)
    full_entry = record_ledger_payment(full_order, str(full_order.total), "cash", full_cashier)
    refund_ledger_payment(full_entry, "5.00", "customer complaint", full_cashier)
    _db.session.refresh(full_order)
    assert full_order.amount_settled == full_order.total - D("5.00")

    # --- seed: a partially-paid ledger-native order (two partial payments,
    # deliberately not summing to the order's full total) -------------------
    # _paid_order's "price" is the pre-tax menu-item price, not Order.total
    # (checkout applies the configured tax rate on top -- see
    # app/services/pricing.py's price()) -- so the Rs.20 left outstanding
    # below is computed from the order's own real total, never hardcoded
    # against the untaxed Rs.60 price.
    partial_order, partial_cashier = _paid_order(price="60.00", qty=1)
    outstanding = D("20.00")
    paid_total = (partial_order.total - outstanding).quantize(D("0.01"))
    first_partial = D("30.00")
    second_partial = (paid_total - first_partial).quantize(D("0.01"))
    record_ledger_payment(partial_order, str(first_partial), "cash", partial_cashier)
    record_ledger_payment(partial_order, str(second_partial), "card", partial_cashier)
    _db.session.refresh(partial_order)
    assert partial_order.amount_settled == paid_total
    assert partial_order.total - partial_order.amount_settled == outstanding

    # --- seed: a legacy payment, already reconciled -------------------------
    # Builds the historical "legacy payments row + backfilled ledger entries"
    # shape scripts/reconcile_payment_ledger.py's own apply_reconciliation
    # produces, but does so directly through model operations only -- this
    # test must never execute reconciliation against any database, only
    # prove backup/restore/audit round-trips data that already has this
    # shape. The legacy row itself (make_legacy_payment, refund_legacy_payment)
    # is the same direct-model-write test convenience tests/conftest.py
    # already provides for exactly this "pre-cutover payments row" shape;
    # the ledger entries and Order.amount_settled below are constructed by
    # hand to match what that script's _apply_one would have written for a
    # payment with this amount/method/refund history, without calling it.
    legacy_order, legacy_cashier = _paid_order(price="25.00", qty=1)
    legacy_payment = make_legacy_payment(legacy_order, legacy_cashier)
    refund_legacy_payment(legacy_payment, "5.00", "customer complaint")

    legacy_payment_entry = PaymentLedgerEntry(
        order_id=legacy_order.id, entry_type=LedgerEntryType.PAYMENT, amount=legacy_payment.amount,
        method=legacy_payment.method, refunded_amount=legacy_payment.refunded_amount,
        corrected_amount=D("0.00"), recorded_by_id=legacy_payment.recorded_by_id,
        created_at=legacy_payment.captured_at,
        reason=f"Historical backfill shape for legacy payments.id={legacy_payment.id} (test-constructed)",
    )
    _db.session.add(legacy_payment_entry)
    _db.session.flush()  # legacy_payment_entry.id needed below for the refund entry's reversed_entry_id

    _db.session.add(PaymentLedgerEntry(
        order_id=legacy_order.id, entry_type=LedgerEntryType.REFUND, amount=-legacy_payment.refunded_amount,
        reversed_entry_id=legacy_payment_entry.id, reason=legacy_payment.refund_reason, recorded_by_id=None,
        created_at=legacy_payment.refunded_at,
    ))

    legacy_order.amount_settled = legacy_payment.amount - legacy_payment.refunded_amount
    _db.session.commit()
    _db.session.refresh(legacy_order)
    assert legacy_order.amount_settled == legacy_payment.amount - legacy_payment.refunded_amount

    pre_report = run_audit(_db.engine)
    assert pre_report.is_clean, f"seeded data must already be clean before any backup/restore: {pre_report.findings}"
    assert pre_report.awaiting_reconciliation_count == 0

    # --- back up the real schema for real -----------------------------------
    key_path = tmp_path / "drill.key"
    generate_key_file(key_path)
    backup_dir = tmp_path / "backups"

    meta = bd.create_backup(
        database_url=mysql_test_url, backup_dir=backup_dir, key_file=key_path, encrypt=True, retention=7,
    )
    assert meta.table_row_counts.get("orders", 0) >= 3
    # 1 payment + 1 refund (full_order) + 2 payment (partial_order) + 1 payment + 1 refund (legacy_order)
    assert meta.table_row_counts.get("payment_ledger_entries", 0) >= 6
    assert meta.table_row_counts.get("payments", 0) >= 1
    backup_path = backup_dir / meta.file_name

    url = make_url(mysql_test_url)

    # --- simulate total data loss: wipe every table in this same disposable
    # database (see the module docstring for why this is the same database,
    # not a different one, and why that still exercises restore_database.py's
    # real, unconditional empty-target gate) ---------------------------------
    wipe_mysql_schema(mysql_test_url)

    def _raw_connect():
        return pymysql.connect(host=url.host, port=url.port or 3306, user=url.username,
                                password=url.password or "", database=url.database)

    conn = _raw_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            assert cur.fetchall() == (), "wipe_mysql_schema must leave zero tables before restore"
    finally:
        conn.close()

    # --- restore for real, into the now-empty database ----------------------
    results = rd.restore_backup(
        backup_path=backup_path, database_url=mysql_test_url, target=url.database, key_file=key_path,
    )
    assert results["orders"][0] == results["orders"][1] >= 3
    assert results["payment_ledger_entries"][0] == results["payment_ledger_entries"][1] >= 6
    assert results["payments"][0] == results["payments"][1] >= 1

    # --- verify the real row-level data is back, byte-for-byte, via a plain
    # connection independent of anything the ORM/audit path below touches ---
    conn = _raw_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT amount_settled FROM orders WHERE id = %s", (full_order.id,))
            assert D(str(cur.fetchone()[0])) == full_order.total - D("5.00")

            cur.execute("SELECT total, amount_settled FROM orders WHERE id = %s", (partial_order.id,))
            restored_total, restored_settled = (D(str(v)) for v in cur.fetchone())
            assert restored_total == partial_order.total
            assert restored_settled == paid_total
            assert restored_total - restored_settled == outstanding

            cur.execute("SELECT refunded_amount FROM payments WHERE id = %s", (legacy_payment.id,))
            assert D(str(cur.fetchone()[0])) == legacy_payment.refunded_amount

            cur.execute(
                "SELECT entry_type, amount FROM payment_ledger_entries WHERE order_id = %s ORDER BY id",
                (full_order.id,),
            )
            rows = cur.fetchall()
            assert [r[0] for r in rows] == ["payment", "refund"]
    finally:
        conn.close()

    # --- the real audit tool against the restored copy, on a genuinely fresh
    # engine/connection (never reused from before the restore, so there is no
    # question of stale pooled connection state against the dropped-and-
    # recreated schema) -------------------------------------------------------
    fresh_engine = sa.create_engine(mysql_test_url)
    try:
        post_report = run_audit(fresh_engine)
    finally:
        fresh_engine.dispose()

    assert post_report.is_clean, f"restored data must still be clean: {post_report.findings}"
    assert post_report.orders_checked >= 3
    assert post_report.awaiting_reconciliation_count == 0
