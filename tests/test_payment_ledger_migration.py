"""Milestone 7.5.1: the e39e6dffcb5d migration -- schema, historical
backfill from the legacy ``payments`` table, and the mandatory downgrade
safeguards (preflight refusal, reconstruction, post-reconstruction
verification).

Follows the pattern in test_ops_migration.py: upgrade to the revision right
before this one, write legacy rows with raw SQL, then run just this
migration and inspect the result directly through the connection (no ORM --
the schema under test is defined by the migration, not by the current
models, by design)."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest
import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app import create_app
from app.extensions import db

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from migrations.versions.e39e6dffcb5d_add_payment_ledger import (  # noqa: E402
    LedgerDowngradeRefused,
    _BACKFILL_PAYMENT_ENTRIES_SQL,
    _BACKFILL_REFUND_ENTRIES_SQL,
    _BACKFILL_SETTLEMENT_SQL,
    _preflight_downgrade,
)

PRE_REVISION = "c2d5f8a1b3e6"


def _app(tmp_path, name: str):
    application = create_app("testing")
    application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / name).as_posix()}"
    return application


def _seed_legacy(conn, *, order_id, user_id=1, total="100.00", refunded="0.00", method="cash",
                  refund_reason=None, with_user=True):
    if with_user:
        conn.execute(sa.text(
            "INSERT OR IGNORE INTO users (id, username, email, password_hash, role, is_active, "
            "session_version, staff_approved, created_at, updated_at) VALUES "
            "(:uid, :un, :em, 'x', 'customer', 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"uid": user_id, "un": f"user{user_id}", "em": f"user{user_id}@example.com"})
    conn.execute(sa.text(
        "INSERT INTO orders (id, user_id, status, subtotal, total, source, created_at, updated_at) "
        "VALUES (:oid, :uid, 'completed', :total, :total, 'online', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ), {"oid": order_id, "uid": user_id, "total": total})
    conn.execute(sa.text(
        "INSERT INTO payments (order_id, method, amount, refunded_amount, captured_at, recorded_by_id, "
        "refunded_at, refund_reason, updated_at) VALUES "
        "(:oid, :method, :total, :refunded, CURRENT_TIMESTAMP, :uid, "
        + ("CURRENT_TIMESTAMP" if float(refunded) > 0 else "NULL") + ", :reason, CURRENT_TIMESTAMP)"
    ), {"oid": order_id, "method": method, "total": total, "refunded": refunded, "uid": user_id,
        "reason": refund_reason})


def _upgraded(tmp_path, name, seed):
    application = _app(tmp_path, name)
    with application.app_context():
        upgrade(revision=PRE_REVISION)
        with db.engine.begin() as conn:
            seed(conn)
        upgrade()
    return application


# --- backfill --------------------------------------------------------------------------


def test_backfill_unrefunded_payment(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="226.00")

    app = _upgraded(tmp_path, "unrefunded.db", seed)
    with app.app_context():
        rows = db.session.execute(sa.text(
            "SELECT entry_type, amount, method, refunded_amount, corrected_amount, reversed_entry_id, "
            "recorded_by_id FROM payment_ledger_entries ORDER BY id"
        )).all()
        assert [tuple(r) for r in rows] == [("payment", 226.0, "cash", 0.0, 0.0, None, 1)]
        settled = db.session.execute(sa.text("SELECT amount_settled FROM orders WHERE id = 1")).scalar()
        assert settled == 226.0
        db.session.remove()
        db.engine.dispose()


def test_backfill_partial_refund(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="200.00", refunded="50.00", refund_reason="cold fries")

    app = _upgraded(tmp_path, "partial.db", seed)
    with app.app_context():
        rows = db.session.execute(sa.text(
            "SELECT entry_type, amount, reversed_entry_id, recorded_by_id, reason FROM payment_ledger_entries "
            "ORDER BY id"
        )).all()
        assert len(rows) == 2
        payment, refund = rows
        assert (payment.entry_type, payment.amount) == ("payment", 200.0)
        assert (refund.entry_type, refund.amount, refund.recorded_by_id, refund.reason) \
            == ("refund", -50.0, None, "cold fries")
        # reversed_entry_id must point at the payment row's own id
        payment_id = db.session.execute(
            sa.text("SELECT id FROM payment_ledger_entries WHERE entry_type = 'payment'")
        ).scalar()
        assert refund.reversed_entry_id == payment_id
        settled = db.session.execute(sa.text("SELECT amount_settled FROM orders WHERE id = 1")).scalar()
        assert settled == 150.0
        db.session.remove()
        db.engine.dispose()


def test_backfill_full_refund_settles_to_zero(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="99.00", refunded="99.00")

    app = _upgraded(tmp_path, "full.db", seed)
    with app.app_context():
        settled = db.session.execute(sa.text("SELECT amount_settled FROM orders WHERE id = 1")).scalar()
        assert settled == 0.0
        n = db.session.execute(sa.text(
            "SELECT COUNT(*) FROM payment_ledger_entries WHERE entry_type = 'refund'"
        )).scalar()
        assert n == 1
        db.session.remove()
        db.engine.dispose()


def test_unpaid_order_keeps_default_amount_settled(tmp_path):
    def seed(conn):
        conn.execute(sa.text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, session_version, "
            "staff_approved, created_at, updated_at) VALUES "
            "(1, 'u', 'u@example.com', 'x', 'customer', 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        conn.execute(sa.text(
            "INSERT INTO orders (id, user_id, status, subtotal, total, source, created_at, updated_at) "
            "VALUES (1, 1, 'pending', 50.00, 50.00, 'online', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))

    app = _upgraded(tmp_path, "unpaid.db", seed)
    with app.app_context():
        settled = db.session.execute(sa.text("SELECT amount_settled FROM orders WHERE id = 1")).scalar()
        assert settled == 0.0
        n = db.session.execute(sa.text("SELECT COUNT(*) FROM payment_ledger_entries")).scalar()
        assert n == 0
        db.session.remove()
        db.engine.dispose()


def test_backfill_is_safe_to_run_again(tmp_path):
    """Re-running the exact backfill statements (simulating a retry) must
    not duplicate ledger rows or re-touch amount_settled incorrectly."""

    def seed(conn):
        _seed_legacy(conn, order_id=1, total="200.00", refunded="50.00")

    app = _upgraded(tmp_path, "rerun.db", seed)
    with app.app_context():
        before = db.session.execute(sa.text("SELECT COUNT(*) FROM payment_ledger_entries")).scalar()
        assert before == 2

        with db.engine.begin() as conn:
            conn.execute(sa.text(_BACKFILL_PAYMENT_ENTRIES_SQL))
            conn.execute(sa.text(_BACKFILL_REFUND_ENTRIES_SQL))
            conn.execute(sa.text(_BACKFILL_SETTLEMENT_SQL))

        after = db.session.execute(sa.text("SELECT COUNT(*) FROM payment_ledger_entries")).scalar()
        assert after == before
        settled = db.session.execute(sa.text("SELECT amount_settled FROM orders WHERE id = 1")).scalar()
        assert settled == 150.0
        db.session.remove()
        db.engine.dispose()


# --- downgrade preflight refusals -------------------------------------------------------


def test_downgrade_refuses_order_with_two_payment_entries(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="100.00")

    app = _upgraded(tmp_path, "multi_payment.db", seed)
    with app.app_context():
        db.session.execute(sa.text(
            "INSERT INTO payment_ledger_entries (order_id, entry_type, amount, method, refunded_amount, "
            "corrected_amount, recorded_by_id, created_at) VALUES "
            "(1, 'payment', 5.00, 'cash', 0, 0, 1, CURRENT_TIMESTAMP)"
        ))
        db.session.commit()
        with db.engine.connect() as conn, pytest.raises(LedgerDowngradeRefused, match="more than one payment ledger entry"):
            _preflight_downgrade(conn)
        db.session.remove()
        db.engine.dispose()


def test_downgrade_refuses_adjustment_or_correction(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="100.00")

    app = _upgraded(tmp_path, "adjustment.db", seed)
    with app.app_context():
        db.session.execute(sa.text(
            "INSERT INTO payment_ledger_entries (order_id, entry_type, amount, recorded_by_id, created_at) "
            "VALUES (1, 'adjustment', 5.00, 1, CURRENT_TIMESTAMP)"
        ))
        db.session.commit()
        with db.engine.connect() as conn, pytest.raises(LedgerDowngradeRefused, match="adjustment/correction"):
            _preflight_downgrade(conn)
        db.session.remove()
        db.engine.dispose()


def test_downgrade_refuses_payment_with_two_refunds(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="100.00", refunded="30.00")

    app = _upgraded(tmp_path, "multi_refund.db", seed)
    with app.app_context():
        payment_id = db.session.execute(
            sa.text("SELECT id FROM payment_ledger_entries WHERE entry_type = 'payment'")
        ).scalar()
        db.session.execute(sa.text(
            "INSERT INTO payment_ledger_entries (order_id, entry_type, amount, reversed_entry_id, created_at) "
            "VALUES (1, 'refund', -10.00, :pid, CURRENT_TIMESTAMP)"
        ), {"pid": payment_id})
        db.session.commit()
        with db.engine.connect() as conn, pytest.raises(LedgerDowngradeRefused, match="more than one linked refund"):
            _preflight_downgrade(conn)
        db.session.remove()
        db.engine.dispose()


def test_downgrade_refuses_refunded_amount_mismatch(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="100.00", refunded="30.00")

    app = _upgraded(tmp_path, "mismatch.db", seed)
    with app.app_context():
        # Desync the payment row's projection from its (single) linked refund.
        db.session.execute(sa.text(
            "UPDATE payment_ledger_entries SET refunded_amount = 99.00 WHERE entry_type = 'payment'"
        ))
        db.session.commit()
        with db.engine.connect() as conn, pytest.raises(LedgerDowngradeRefused, match="does not match"):
            _preflight_downgrade(conn)
        db.session.remove()
        db.engine.dispose()


def test_downgrade_refuses_legacy_orphan_payment(tmp_path):
    """A payments row created after the migration ran (hypothetically, by
    legacy code nothing in this milestone touches) with no ledger
    counterpart must block downgrade rather than silently lose it."""

    def seed(conn):
        _seed_legacy(conn, order_id=1, total="100.00")

    app = _upgraded(tmp_path, "orphan.db", seed)
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO users (id, username, email, password_hash, role, is_active, session_version, "
                "staff_approved, created_at, updated_at) VALUES "
                "(2, 'u2', 'u2@example.com', 'x', 'customer', 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO orders (id, user_id, status, subtotal, total, source, created_at, updated_at) "
                "VALUES (2, 2, 'completed', 10.00, 10.00, 'online', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO payments (order_id, method, amount, refunded_amount, captured_at, recorded_by_id, "
                "updated_at) VALUES (2, 'cash', 10.00, 0, CURRENT_TIMESTAMP, 2, CURRENT_TIMESTAMP)"
            ))
        with db.engine.connect() as conn, pytest.raises(LedgerDowngradeRefused, match="no corresponding ledger payment"):
            _preflight_downgrade(conn)
        db.session.remove()
        db.engine.dispose()


def test_downgrade_command_exits_nonzero_on_refusal(tmp_path):
    """End-to-end check that the preflight refusal actually stops
    ``flask db downgrade`` and leaves the schema untouched: flask_migrate's
    CLI wrapper catches the RuntimeError and calls sys.exit(1) rather than
    letting LedgerDowngradeRefused propagate -- see the five tests above for
    the refusal reasons themselves, tested directly against
    _preflight_downgrade, whose message flask_migrate logs before exiting
    (see 'ERROR [flask_migrate] Error: ...' in this test's own captured
    stderr when it is run)."""

    def seed(conn):
        _seed_legacy(conn, order_id=1, total="100.00")

    app = _upgraded(tmp_path, "refusal_wiring.db", seed)
    with app.app_context():
        db.session.execute(sa.text(
            "INSERT INTO payment_ledger_entries (order_id, entry_type, amount, recorded_by_id, created_at) "
            "VALUES (1, 'adjustment', 5.00, 1, CURRENT_TIMESTAMP)"
        ))
        db.session.commit()
        with pytest.raises(SystemExit):
            downgrade(revision=PRE_REVISION)
        assert "payment_ledger_entries" in sa.inspect(db.engine).get_table_names()
        assert "amount_settled" in {c["name"] for c in sa.inspect(db.engine).get_columns("orders")}
        db.session.remove()
        db.engine.dispose()


# --- successful downgrade + reconstruction ----------------------------------------------


def test_successful_downgrade_reconstructs_payments_and_drops_ledger(tmp_path):
    def seed(conn):
        _seed_legacy(conn, order_id=1, total="200.00", refunded="50.00", refund_reason="cold fries")

    app = _upgraded(tmp_path, "roundtrip.db", seed)
    with app.app_context():
        # Simulate drift: the ledger's payment entry now disagrees with the
        # still-present legacy row (as if something had changed only the
        # ledger since -- reconstruction must make payments match the
        # ledger, not assume payments is already right).
        db.session.execute(sa.text(
            "UPDATE payment_ledger_entries SET method = 'card' WHERE entry_type = 'payment'"
        ))
        db.session.commit()

        downgrade(revision=PRE_REVISION)

        inspector = sa.inspect(db.engine)
        assert "payment_ledger_entries" not in inspector.get_table_names()
        assert "amount_settled" not in {c["name"] for c in inspector.get_columns("orders")}

        method, amount, refunded = db.session.execute(sa.text(
            "SELECT method, amount, refunded_amount FROM payments WHERE order_id = 1"
        )).one()
        assert (method, amount, refunded) == ("card", 200.0, 50.0)
        db.session.remove()
        db.engine.dispose()


def test_downgrade_on_untouched_ledger_is_a_clean_noop_reconstruction(tmp_path):
    """No drift case: payments is already correct, reconstruction just
    confirms it and the round trip loses nothing."""

    def seed(conn):
        _seed_legacy(conn, order_id=1, total="75.50")

    app = _upgraded(tmp_path, "clean.db", seed)
    with app.app_context():
        downgrade(revision=PRE_REVISION)
        amount = db.session.execute(sa.text("SELECT amount FROM payments WHERE order_id = 1")).scalar()
        assert amount == 75.5
        count = db.session.execute(sa.text("SELECT COUNT(*) FROM payments")).scalar()
        assert count == 1
        db.session.remove()
        db.engine.dispose()


# --- real MySQL verification ------------------------------------------------------------
#
# Runs only when TEST_DATABASE_URL points at MySQL (skipped on the default
# SQLite run). Exercises what SQLite cannot stand in for: the ON DUPLICATE
# KEY UPDATE branch of _reconstruct_payments_from_ledger, and the same raw
# SQL (NOT EXISTS / correlated subqueries / GROUP BY ... HAVING) actually
# running under MySQL's parser and InnoDB rather than SQLite's. The
# `_fresh_schema_for_mysql_migration_tests` fixture in conftest.py wipes
# TEST_DATABASE_URL's schema before each test in this module automatically
# (its name matches "migration").

def _mysql_test_url():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("mysql"):
        pytest.skip("MySQL verification needs TEST_DATABASE_URL=mysql+pymysql://...@host/..._test")
    return url


def _mysql_app():
    application = create_app("testing")
    application.config["SQLALCHEMY_DATABASE_URI"] = _mysql_test_url()
    return application


def _seed_legacy_mysql(conn, *, order_id, user_id=1, total="100.00", refunded="0.00",
                        refund_reason=None):
    conn.execute(sa.text(
        "INSERT IGNORE INTO users (id, username, email, password_hash, role, is_active, "
        "session_version, staff_approved, created_at, updated_at) VALUES "
        "(:uid, :un, :em, 'x', 'customer', 1, 0, 0, NOW(), NOW())"
    ), {"uid": user_id, "un": f"user{user_id}", "em": f"user{user_id}@example.com"})
    conn.execute(sa.text(
        "INSERT INTO orders (id, user_id, status, subtotal, total, source, created_at, updated_at) "
        "VALUES (:oid, :uid, 'completed', :total, :total, 'online', NOW(), NOW())"
    ), {"oid": order_id, "uid": user_id, "total": total})
    conn.execute(sa.text(
        "INSERT INTO payments (order_id, method, amount, refunded_amount, captured_at, recorded_by_id, "
        "refunded_at, refund_reason, updated_at) VALUES "
        "(:oid, 'cash', :total, :refunded, NOW(), :uid, "
        + ("NOW()" if float(refunded) > 0 else "NULL") + ", :reason, NOW())"
    ), {"oid": order_id, "total": total, "refunded": refunded, "uid": user_id, "reason": refund_reason})


def test_mysql_full_round_trip_payment_and_refund():
    application = _mysql_app()
    with application.app_context():
        upgrade(revision=PRE_REVISION)
        with db.engine.begin() as conn:
            _seed_legacy_mysql(conn, order_id=1, total="200.00", refunded="50.00", refund_reason="cold fries")

        upgrade()
        assert db.session.execute(sa.text("SELECT amount_settled FROM orders WHERE id = 1")).scalar() == 150.0
        rows = db.session.execute(sa.text(
            "SELECT entry_type, amount FROM payment_ledger_entries ORDER BY id"
        )).all()
        assert [tuple(r) for r in rows] == [("payment", 200.0), ("refund", -50.0)]

        # Drift the ledger's payment entry so reconstruction has to do real work.
        db.session.execute(sa.text("UPDATE payment_ledger_entries SET method = 'wallet' WHERE entry_type = 'payment'"))
        db.session.commit()

        downgrade(revision=PRE_REVISION)
        inspector = sa.inspect(db.engine)
        assert "payment_ledger_entries" not in inspector.get_table_names()
        method, amount, refunded = db.session.execute(sa.text(
            "SELECT method, amount, refunded_amount FROM payments WHERE order_id = 1"
        )).one()
        assert (method, amount, refunded) == ("wallet", 200.0, 50.0)
        db.session.remove()
        db.engine.dispose()


def test_mysql_downgrade_refuses_on_adjustment():
    application = _mysql_app()
    with application.app_context():
        upgrade(revision=PRE_REVISION)
        with db.engine.begin() as conn:
            _seed_legacy_mysql(conn, order_id=1, total="100.00")
        upgrade()

        db.session.execute(sa.text(
            "INSERT INTO payment_ledger_entries (order_id, entry_type, amount, recorded_by_id, created_at) "
            "VALUES (1, 'adjustment', 5.00, 1, NOW())"
        ))
        db.session.commit()

        with db.engine.connect() as conn, pytest.raises(LedgerDowngradeRefused, match="adjustment/correction"):
            _preflight_downgrade(conn)
        assert "payment_ledger_entries" in sa.inspect(db.engine).get_table_names()
        db.session.remove()
        db.engine.dispose()
