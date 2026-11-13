"""O1: migration 4c7e2a9f1b38 (local organization, branch and installation
identity) -- fresh database, populated existing installation, downgrade and
re-upgrade.

Runs on the TEST_DATABASE_URL MySQL database when one is set (conftest wipes
it before each test because this module's name contains "migration"; the
fixture below wipes it again afterwards), otherwise on a scratch SQLite
file. Only this one revision is downgraded, so known issue #32 (full
downgrade to base on MySQL) does not apply.

The last test runs only on MySQL. CI's MySQL job runs this file on its own
and fails unless every test passes with none skipped, so this file can never
"pass" there while silently running on SQLite.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from flask_migrate import downgrade, upgrade
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models.customer_preference import CustomerPreference
from app.models.enums import Cuisine, SpiceLevel
from app.models.ingredient import Ingredient, StockUnit
from app.models.reservation import Reservation, ReservationStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.stock_movement import MovementType, StockMovement
from app.services.installation import get_installation
from app.services.payment_ledger import record_ledger_payment, refund_ledger_payment
from config import TestingConfig
from scripts.audit_payment_ledger import run_audit
from tests.conftest import make_legacy_payment, rejected_by_check_constraint, wipe_mysql_schema
from tests.test_payments import member
from tests.test_restaurant_ops import customer, place

# O1's own revision. Every upgrade() here stops at it, so these tests keep
# exercising O1 in isolation now that later revisions (O2) exist.
HEAD = "4c7e2a9f1b38"
PREVIOUS = "e39e6dffcb5d"
IDENTITY_TABLES = {"organizations", "branches", "installation"}
MYSQL_URL = os.environ.get("TEST_DATABASE_URL", "") if os.environ.get("TEST_DATABASE_URL", "").startswith("mysql") else ""


@pytest.fixture(autouse=True)
def _leave_shared_mysql_schema_empty():
    yield
    wipe_mysql_schema(MYSQL_URL)


def _app(tmp_path, name="identity.db"):
    """The MySQL test database when configured, else a scratch SQLite file.
    The URI must be patched on the config class *before* create_app(): the
    engine is bound inside it (see tests/test_payment_ledger_migration.py)."""
    if MYSQL_URL:
        return create_app("testing")
    original = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{(tmp_path / name).as_posix()}"
    try:
        return create_app("testing")
    finally:
        TestingConfig.SQLALCHEMY_DATABASE_URI = original


def _release():
    """End the session's transaction and return its connection, so Alembic's
    own connection can take MySQL's metadata locks for its DDL."""
    db.session.commit()
    db.session.remove()


def _tables():
    return set(sa.inspect(db.engine).get_table_names())


def _snapshot(tables):
    """Every row of every listed table, plus its row count and highest id."""
    snap = {}
    with db.engine.connect() as conn:
        for table in sorted(tables):
            rows = sorted((tuple(r) for r in conn.execute(sa.text(f"SELECT * FROM {table}"))), key=repr)
            columns = {c["name"] for c in sa.inspect(conn).get_columns(table)}
            max_id = conn.execute(sa.text(f"SELECT MAX(id) FROM {table}")).scalar() if "id" in columns else None
            snap[table] = {"count": len(rows), "max_id": max_id, "rows": rows}
    return snap


def _identity_rows():
    with db.engine.connect() as conn:
        return {
            "organizations": conn.execute(sa.text("SELECT id, public_id, name, hq_id FROM organizations")).all(),
            "branches": conn.execute(sa.text(
                "SELECT id, organization_id, public_id, name, hq_id FROM branches")).all(),
            "installation": conn.execute(sa.text("SELECT id, public_id, branch_id FROM installation")).all(),
        }


def _assert_one_valid_identity(expected_name):
    rows = _identity_rows()
    assert len(rows["organizations"]) == len(rows["branches"]) == len(rows["installation"]) == 1
    (org_id, org_uuid, org_name, org_hq), = rows["organizations"]
    (branch_id, branch_org_id, branch_uuid, branch_name, branch_hq), = rows["branches"]
    (inst_id, inst_uuid, inst_branch_id), = rows["installation"]
    assert (org_name, branch_name) == (expected_name, expected_name)
    assert org_hq is None and branch_hq is None
    assert branch_org_id == org_id and inst_branch_id == branch_id and inst_id == 1
    for value in (org_uuid, branch_uuid, inst_uuid):
        assert str(uuid.UUID(value)) == value
    assert len({org_uuid, branch_uuid, inst_uuid}) == 3
    return rows


def _ledger_audit():
    report = run_audit(db.engine)
    return (report.is_clean, report.orders_checked, report.awaiting_reconciliation_count,
            sorted((f.rule, f.order_id, f.detail) for f in report.findings))


def _seed_existing_installation():
    """A restaurant that has been trading: accounts, menu, orders, ledger
    payments and a refund, a legacy payment, tables, a reservation,
    inventory and a customer preference -- written through the same models
    and services the application uses, at the pre-O1 schema."""
    db.session.execute(sa.text("UPDATE restaurant_profile SET name = 'Cafe Himalaya' WHERE id = 1"))
    db.session.commit()

    diner = customer(username="mig_diner")
    cashier = member(name="mig_cashier")
    paid = place(diner, price="40.00", qty=1)
    entry = record_ledger_payment(paid, str(paid.total), "cash", cashier)
    refund_ledger_payment(entry, "5.00", "cold", cashier)
    partly = place(customer(username="mig_diner2"), price="60.00", qty=2)
    record_ledger_payment(partly, "30.00", "card", cashier)
    make_legacy_payment(place(customer(username="mig_diner3"), price="25.00", qty=1), cashier)

    table = RestaurantTable(name="T-mig", capacity=4, status=TableStatus.AVAILABLE)
    db.session.add(table)
    db.session.flush()
    db.session.add(Reservation(user_id=diner.id, table_id=table.id, reservation_date=date.today() + timedelta(days=3),
                               reservation_time=time(19, 0), guest_count=2, status=ReservationStatus.CONFIRMED))
    rice = Ingredient(name="Rice", unit=StockUnit.KILOGRAM, current_quantity=Decimal("10"))
    db.session.add(rice)
    db.session.flush()
    db.session.add(StockMovement(ingredient_id=rice.id, movement_type=MovementType.PURCHASE,
                                 quantity_change=Decimal("10"), quantity_after=Decimal("10"), actor_id=cashier.id))
    db.session.add(CustomerPreference(user_id=diner.id, cuisine_preference=Cuisine.INDIAN,
                                      spice_preference=SpiceLevel.MEDIUM))
    db.session.commit()


# --- fresh installation --------------------------------------------------------------------


def test_fresh_database_gets_exactly_one_identity(tmp_path):
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=HEAD)
            assert db.session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == HEAD
            rows = _assert_one_valid_identity("Quick Junction")

            identity = get_installation()
            assert identity.installation_public_id == rows["installation"][0][1]
            assert identity.branch_public_id == rows["branches"][0][2]
            assert identity.organization_public_id == rows["organizations"][0][1]
            assert identity.hq_linked is False
        finally:
            db.session.remove()
            db.engine.dispose()


def test_missing_restaurant_profile_falls_back_to_the_default_name(tmp_path):
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=PREVIOUS)
            db.session.execute(sa.text("DELETE FROM restaurant_profile"))
            _release()
            upgrade(revision=HEAD)
            _assert_one_valid_identity("Quick Junction")
        finally:
            db.session.remove()
            db.engine.dispose()


def test_identity_survives_restarts_reads_and_cli_without_changing(tmp_path):
    first = _app(tmp_path)
    with first.app_context():
        upgrade(revision=HEAD)
        before = _identity_rows()
        identity = get_installation()
        for _ in range(3):
            assert get_installation() == identity
        result = first.test_cli_runner().invoke(args=["installation", "show"])
        assert result.exit_code == 0, result.output
        assert f"Installation ID:    {identity.installation_public_id}" in result.output
        assert "Organization:       Quick Junction" in result.output
        assert "HQ linked:          no" in result.output
        _release()
        db.engine.dispose()

    restarted = _app(tmp_path)  # a new application process against the same database
    with restarted.app_context():
        try:
            assert get_installation() == identity
            _release()
            upgrade(revision=HEAD)  # already there: a no-op, never a second identity
            assert _identity_rows() == before
        finally:
            db.session.remove()
            db.engine.dispose()


# --- existing populated installation -----------------------------------------------------


def test_existing_installation_upgrade_downgrade_and_reupgrade(tmp_path):
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=PREVIOUS)
            _seed_existing_installation()
            _release()

            existing = _tables() - {"alembic_version"}
            assert not existing & IDENTITY_TABLES
            before = _snapshot(existing)
            ledger_before = _ledger_audit()
            # 2 ledger payments + 1 refund; the legacy payment has no ledger entry.
            assert before["payment_ledger_entries"]["count"] == 3
            assert before["orders"]["count"] == 3 and before["payments"]["count"] == 1

            # --- upgrade: one identity, nothing else touched ---
            upgrade(revision=HEAD)
            assert _tables() == existing | IDENTITY_TABLES | {"alembic_version"}
            rows = _assert_one_valid_identity("Cafe Himalaya")
            assert _snapshot(existing) == before
            assert _ledger_audit() == ledger_before
            identity = get_installation()
            assert identity.installation_public_id == rows["installation"][0][1]
            _release()

            # --- downgrade: only the three new tables go ---
            downgrade(revision=PREVIOUS)
            assert _tables() == existing | {"alembic_version"}
            assert _snapshot(existing) == before
            assert _ledger_audit() == ledger_before
            with db.engine.connect() as conn:
                assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == PREVIOUS

            # --- re-upgrade: works, again exactly one identity ---
            upgrade(revision=HEAD)
            again = _assert_one_valid_identity("Cafe Himalaya")
            assert _snapshot(existing) == before
            # A downgrade discards the identity; re-upgrading mints a new one.
            assert again["installation"][0][1] != rows["installation"][0][1]
        finally:
            db.session.remove()
            db.engine.dispose()


# --- MySQL only ------------------------------------------------------------------------------


def test_mysql_migrated_identity_constraints_are_enforced(tmp_path):
    if not MYSQL_URL:
        pytest.skip("needs TEST_DATABASE_URL=mysql+pymysql://...*_test (CI's MySQL job requires it to run)")
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=HEAD)
            assert db.engine.dialect.name == "mysql"
            _assert_one_valid_identity("Quick Junction")
            with db.engine.connect() as conn:
                branch_id = conn.execute(sa.text("SELECT id FROM branches")).scalar()
                with rejected_by_check_constraint():
                    conn.execute(sa.text("INSERT INTO installation (id, public_id, branch_id) VALUES (2, :p, :b)"),
                                 {"p": str(uuid.uuid4()), "b": branch_id})
                conn.rollback()
                with pytest.raises(IntegrityError):
                    conn.execute(sa.text("INSERT INTO installation (id, public_id, branch_id) VALUES (1, :p, :b)"),
                                 {"p": str(uuid.uuid4()), "b": branch_id})
                conn.rollback()
                with pytest.raises(IntegrityError):
                    conn.execute(sa.text("DELETE FROM organizations"))
                conn.rollback()
                with pytest.raises(IntegrityError):
                    conn.execute(sa.text("DELETE FROM branches"))
                conn.rollback()
                with pytest.raises(IntegrityError):
                    conn.execute(sa.text("INSERT INTO organizations (public_id, name) "
                                         "SELECT public_id, 'dup' FROM organizations"))
                conn.rollback()
            _assert_one_valid_identity("Quick Junction")
        finally:
            db.session.remove()
            db.engine.dispose()
