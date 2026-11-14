"""O2: migration 4f1d8b2c6a90 (organization membership and branch
assignment) -- fresh database, populated existing installation (every kind
of staff account), refusal on an unexpected identity, downgrade and
re-upgrade.

Runs on the TEST_DATABASE_URL MySQL database when one is set (conftest wipes
it before each test because this module's name contains "migration"; the
fixture below wipes it again afterwards), otherwise on a scratch SQLite
file. Only O2's own revision is downgraded, so known issue #32 does not
apply. The last test runs only on MySQL; CI's O2 guard requires it to pass.

Every ``upgrade()`` here stops at O2's own revision, so these tests keep
exercising O2 alone now that O3.3 (9a3e6c1d7b54) exists -- the same pinning
O2 applied to O1's tests. At that revision the ORM models (which since O3.3
map the role columns) and the services built on them cannot run, so the
checks after an upgrade read and write with plain SQL: ``_o2_problems``
applies ``check_consistency``'s four O2 rules, and the revocation is written
as ``revoke_membership`` would write it.
"""

from __future__ import annotations

import importlib
import secrets
from datetime import timedelta

import pytest
import sqlalchemy as sa
from flask_migrate import downgrade, upgrade
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.account_token import AccountToken, TokenPurpose
from app.models.user import Role, User
from app.utils.clock import db_now
from app.utils.security import hash_password
from tests.conftest import rejected_by_check_constraint, wipe_mysql_schema
from tests.test_installation_migration import (
    MYSQL_URL, _app, _ledger_audit, _release, _seed_existing_installation, _snapshot, _tables)

_MIGRATION = importlib.import_module("migrations.versions.4f1d8b2c6a90_add_organization_membership")
O2 = "4f1d8b2c6a90"
O1 = "4c7e2a9f1b38"
O2_TABLES = {"organization_memberships", "branch_assignments"}
O2_EVENTS = ("membership_created", "membership_revoked", "membership_reinstated",
             "branch_assignment_created", "branch_assignment_revoked", "branch_assignment_reinstated")


@pytest.fixture(autouse=True)
def _leave_shared_mysql_schema_empty():
    yield
    wipe_mysql_schema(MYSQL_URL)


def _scalar(sql, **params):
    with db.engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()


def _rows(sql, **params):
    with db.engine.connect() as conn:
        return conn.execute(sa.text(sql), params).all()


def _o2_problems():
    """check_consistency()'s four O2 rules, in SQL (see the module docstring)."""
    problems = [f"missing_membership {r[0]}" for r in _rows(
        "SELECT u.id FROM users u LEFT JOIN organization_memberships m ON m.user_id = u.id "
        "WHERE u.role IN ('admin', 'staff') AND m.id IS NULL")]
    problems += [f"customer_member {r[0]}" for r in _rows(
        "SELECT m.id FROM organization_memberships m JOIN users u ON u.id = m.user_id "
        "WHERE u.role NOT IN ('admin', 'staff')")]
    problems += [f"missing_assignment {r[0]}" for r in _rows(
        "SELECT m.id FROM organization_memberships m LEFT JOIN branch_assignments a ON a.membership_id = m.id "
        "WHERE m.status = 'active' AND a.id IS NULL")]
    problems += [f"active_assignment_under_revoked_membership {r[0]}" for r in _rows(
        "SELECT a.id FROM branch_assignments a JOIN organization_memberships m ON m.id = a.membership_id "
        "WHERE m.status = 'revoked' AND a.status = 'active'")]
    return problems


def _has_branch_index():
    return any(i["name"] == "uq_branches_id_organization" for i in sa.inspect(db.engine).get_indexes("branches"))


def _account(username, role, *, active=True, approved=None):
    user = User(username=username, email=f"{username}@example.com", password_hash=hash_password("x-password-1"),
                role=role, is_active=active, staff_approved=(role == Role.STAFF) if approved is None else approved)
    db.session.add(user)
    db.session.flush()
    return user


def _seed_every_kind_of_account():
    """Accounts in every state the pre-O2 code can produce, besides the
    customers and the approved staff cashier _seed_existing_installation adds."""
    _account("mig_admin", Role.ADMIN)
    _account("mig_admin_off", Role.ADMIN, active=False)
    _account("mig_pending", Role.STAFF, approved=False)          # pre-M4 self-registration, awaiting approval
    _account("mig_staff_off", Role.STAFF, active=False)          # deactivated
    invited = _account("mig_invited", Role.STAFF, active=False)  # invitation not yet accepted
    db.session.add(AccountToken(user_id=invited.id, purpose=TokenPurpose.STAFF_INVITE,
                                token_hash=secrets.token_hex(32), expires_at=db_now() + timedelta(hours=48)))
    _account("mig_customer", Role.CUSTOMER)
    db.session.commit()


# --- fresh installation -----------------------------------------------------------------------


def test_fresh_database_has_no_memberships_until_accounts_exist(tmp_path):
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=O2)
            assert _scalar("SELECT version_num FROM alembic_version") == O2
            assert O2_TABLES <= _tables() and _has_branch_index()
            assert _scalar("SELECT COUNT(*) FROM organization_memberships") == 0
            assert _scalar("SELECT COUNT(*) FROM branch_assignments") == 0
            assert _o2_problems() == []
        finally:
            db.session.remove()
            db.engine.dispose()


# --- existing populated installation ----------------------------------------------------------


def test_existing_installation_backfill_downgrade_and_reupgrade(tmp_path):
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=O1)
            _seed_existing_installation()
            _seed_every_kind_of_account()
            _release()

            existing = _tables() - {"alembic_version"}
            assert not existing & O2_TABLES
            before = _snapshot(existing)
            ledger_before = _ledger_audit()
            members_expected = {r[0] for r in _rows("SELECT id FROM users WHERE role IN ('admin', 'staff')")}
            customers = {r[0] for r in _rows("SELECT id FROM users WHERE role = 'customer'")}
            assert len(members_expected) == 6 and len(customers) == 4  # incl. pending, inactive and invited staff
            organization_id = _scalar("SELECT id FROM organizations")
            branch_id = _scalar("SELECT id FROM branches")

            # --- upgrade: every admin/staff account, never a customer, nothing else touched ---
            upgrade(revision=O2)
            memberships = _rows("SELECT id, organization_id, user_id, status, created_by_id, revoked_at, "
                                "revoked_by_id FROM organization_memberships ORDER BY id")
            assert {m.user_id for m in memberships} == members_expected
            assert not {m.user_id for m in memberships} & customers
            assert all((m.organization_id, m.status, m.created_by_id, m.revoked_at, m.revoked_by_id)
                       == (organization_id, "active", None, None, None) for m in memberships)
            assert [m.user_id for m in memberships] == sorted(members_expected)  # ids follow account ids
            assignments = _rows("SELECT organization_id, membership_id, branch_id, status, created_by_id "
                                "FROM branch_assignments ORDER BY id")
            assert [(a.organization_id, a.membership_id, a.branch_id, a.status, a.created_by_id)
                    for a in assignments] == [(organization_id, m.id, branch_id, "active", None)
                                              for m in memberships]
            assert _snapshot(existing) == before  # accounts, audit log, orders, ledger, identity: identical
            assert _ledger_audit() == ledger_before
            assert _scalar("SELECT COUNT(*) FROM audit_logs WHERE event_type LIKE 'membership_%' "
                           "OR event_type LIKE 'branch_assignment_%'") == 0  # the backfill writes no audit rows
            assert _o2_problems() == []
            _release()

            # --- use it: an O2 audit row and a revocation the downgrade must remove (written as
            # revoke_membership writes them; the service needs the head models -- see module docstring) ---
            membership_id = _scalar("SELECT MIN(id) FROM organization_memberships")
            with db.engine.begin() as conn:
                conn.execute(sa.text("UPDATE branch_assignments SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP "
                                     "WHERE membership_id = :m"), {"m": membership_id})
                conn.execute(sa.text("UPDATE organization_memberships SET status = 'revoked', "
                                     "revoked_at = CURRENT_TIMESTAMP WHERE id = :m"), {"m": membership_id})
                conn.execute(sa.text("INSERT INTO audit_logs (event_type, success, created_at) "
                                     "VALUES ('membership_revoked', 1, CURRENT_TIMESTAMP)"))
            assert _o2_problems() == []
            assert _scalar("SELECT COUNT(*) FROM audit_logs WHERE event_type = 'membership_revoked'") == 1

            # --- downgrade: only O2's objects and audit events go ---
            downgrade(revision=O1)
            assert _tables() == existing | {"alembic_version"}
            assert not _has_branch_index()
            assert _snapshot(existing) == before
            assert _ledger_audit() == ledger_before
            assert _scalar("SELECT version_num FROM alembic_version") == O1
            with pytest.raises(Exception, match="(?i)check constraint"):
                with db.engine.begin() as conn:  # the allow-list is narrowed again
                    conn.execute(sa.text("INSERT INTO audit_logs (event_type, success, created_at) "
                                         "VALUES ('membership_created', 1, CURRENT_TIMESTAMP)"))

            # --- re-upgrade: backfilled afresh, all active (the revocation is gone) ---
            upgrade(revision=O2)
            assert {r[0] for r in _rows("SELECT user_id FROM organization_memberships")} == members_expected
            assert {r[0] for r in _rows("SELECT status FROM organization_memberships")} == {"active"}
            assert _snapshot(existing) == before
        finally:
            db.session.remove()
            db.engine.dispose()


@pytest.mark.parametrize("extra", ["branch", "organization"])
def test_unexpected_identity_refuses_the_upgrade_before_changing_anything(tmp_path, extra):
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=O1)
            organization_id = _scalar("SELECT id FROM organizations")
            with db.engine.begin() as conn:
                if extra == "organization":
                    conn.execute(sa.text("INSERT INTO organizations (public_id, name) VALUES (:p, 'Second')"),
                                 {"p": "0b1c2d3e-0000-4000-8000-000000000001"})
                else:
                    conn.execute(sa.text("INSERT INTO branches (organization_id, public_id, name) "
                                         "VALUES (:o, :p, 'Second')"),
                                 {"o": organization_id, "p": "0b1c2d3e-0000-4000-8000-000000000002"})
            existing = _tables()
            before = _snapshot(existing - {"alembic_version"})

            # The migration's own guard, called directly for its message ...
            with db.engine.connect() as conn, pytest.raises(_MIGRATION.MembershipBackfillRefused,
                                                            match="exactly one organization and one branch"):
                _MIGRATION._local_organization_and_branch(conn)
            # ... and through Alembic, which Flask-Migrate reports as SystemExit.
            with pytest.raises(SystemExit):
                upgrade(revision=O2)
            assert _tables() == existing and not _has_branch_index()
            assert _scalar("SELECT version_num FROM alembic_version") == O1
            assert _snapshot(existing - {"alembic_version"}) == before
        finally:
            db.session.remove()
            db.engine.dispose()


# --- MySQL only ----------------------------------------------------------------------------------


def test_mysql_migrated_constraints_are_enforced(tmp_path):
    if not MYSQL_URL:
        pytest.skip("needs TEST_DATABASE_URL=mysql+pymysql://...*_test (CI's MySQL job requires it to run)")
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=O1)
            with db.engine.begin() as conn:
                conn.execute(sa.text("INSERT INTO users (username, email, password_hash, role, is_active, "
                                     "session_version, staff_approved, created_at, updated_at) VALUES "
                                     "('mcook', 'mcook@example.com', 'x', 'staff', 1, 0, 1, NOW(), NOW())"))
            upgrade(revision=O2)
            assert db.engine.dialect.name == "mysql" and _has_branch_index()
            membership_id, organization_id, user_id = _rows(
                "SELECT id, organization_id, user_id FROM organization_memberships")[0]
            with db.engine.connect() as conn:
                conn.execute(sa.text("INSERT INTO organizations (public_id, name) VALUES "
                                     "('0b1c2d3e-0000-4000-8000-000000000003', 'Other')"))
                other_org = conn.execute(sa.text("SELECT id FROM organizations WHERE name = 'Other'")).scalar()
                conn.execute(sa.text("INSERT INTO branches (organization_id, public_id, name) VALUES "
                                     "(:o, '0b1c2d3e-0000-4000-8000-000000000004', 'Other')"), {"o": other_org})
                other_branch = conn.execute(sa.text("SELECT id FROM branches WHERE name = 'Other'")).scalar()
                conn.commit()

                insert = sa.text("INSERT INTO branch_assignments (organization_id, membership_id, branch_id, "
                                 "status) VALUES (:o, :m, :b, 'active')")
                for claimed in (organization_id, other_org):  # cross-organization: a composite key refuses
                    with pytest.raises(IntegrityError):
                        conn.execute(insert, {"o": claimed, "m": membership_id, "b": other_branch})
                    conn.rollback()
                with pytest.raises(IntegrityError):  # duplicate membership
                    conn.execute(sa.text("INSERT INTO organization_memberships (organization_id, user_id, status) "
                                         "VALUES (:o, :u, 'active')"), {"o": organization_id, "u": user_id})
                conn.rollback()
                with rejected_by_check_constraint():  # status allow-list
                    conn.execute(sa.text("UPDATE organization_memberships SET status = 'suspended'"))
                conn.rollback()
                with rejected_by_check_constraint():  # revoked without revoked_at
                    conn.execute(sa.text("UPDATE branch_assignments SET status = 'revoked'"))
                conn.rollback()
                with pytest.raises(IntegrityError):  # RESTRICT
                    conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": user_id})
                conn.rollback()
            assert _scalar("SELECT COUNT(*) FROM branch_assignments") == 1
        finally:
            db.session.remove()
            db.engine.dispose()
