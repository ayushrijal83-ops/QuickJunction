"""O3.3: migration 9a3e6c1d7b54 (organization and branch membership roles).

Fresh and populated installations; every pre-DDL refusal (R1-R9), including
orphan rows written with foreign-key enforcement off; the backfill's own
verification; downgrade, its refusals and its retry; and a failure after the
first ALTER TABLE (state X) or after the backfill (state Y), with the
documented recovery and a retry -- each run through the real Flask-Migrate
environment (migrations/env.py), so the partial states recorded here are
the ones an operator would actually meet.

Runs on the TEST_DATABASE_URL MySQL database when one is set (conftest wipes
it before each test, this module's fixture after), otherwise on a scratch
SQLite file. The ``test_mysql_*`` tests run only on MySQL; CI's O3.3 guard
requires the whole module and tests/test_membership_roles.py to pass there
with nothing skipped.
"""

from __future__ import annotations

import contextlib
import importlib
import shutil

import pytest
import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app.extensions import db
from app.models.organization import BranchAssignment, OrganizationMembership
from app.models.user import Role, User
from app.services import organization as org
from app.services.auth import RegistrationInput, create_admin
from app.utils.permissions import auth_context_for_user
from tests.conftest import make_user, rejected_by_check_constraint, wipe_mysql_schema
from tests.test_installation_migration import (
    MYSQL_URL, _app, _ledger_audit, _release, _seed_existing_installation, _snapshot, _tables)
from tests.test_organization_migration import _account, _seed_every_kind_of_account

MIG = importlib.import_module("migrations.versions.9a3e6c1d7b54_add_membership_roles")
O3 = "9a3e6c1d7b54"
O2 = "4f1d8b2c6a90"
O1 = "4c7e2a9f1b38"
O2_TABLES = {"organization_memberships", "branch_assignments"}
REVOKED_AT = "2026-11-01 10:00:00"
PASSWORD = "correct-horse-1"

NO_ROLE_SCHEMA = {"organization_role": (False, False), "branch_role": (False, False)}
FULL_ROLE_SCHEMA = {"organization_role": (True, True), "branch_role": (True, True)}
STATE_X = {"organization_role": (True, True), "branch_role": (False, False)}   # DDL-2 failed


@pytest.fixture(autouse=True)
def _leave_shared_mysql_schema_empty():
    yield
    wipe_mysql_schema(MYSQL_URL)


@pytest.fixture
def scratch(tmp_path):
    """The migration app (MySQL when configured, else a SQLite file), in its
    own app context, cleaned up afterwards."""
    application = _app(tmp_path, "roles.db")
    with application.app_context():
        try:
            yield tmp_path / "roles.db"
        finally:
            db.session.remove()
            db.engine.dispose()


# --- helpers ----------------------------------------------------------------------------------------


def _scalar(sql, **params):
    with db.engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()


def _rows(sql, **params):
    with db.engine.connect() as conn:
        return conn.execute(sa.text(sql), params).all()


def _exec(sql, **params):
    with db.engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def _version():
    return _scalar("SELECT version_num FROM alembic_version")


def _role_schema():
    with db.engine.connect() as conn:
        return {rc.column: (MIG.column_present(conn, rc), MIG.check_present(conn, rc)) for rc in MIG.ROLE_COLUMNS}


def _lifecycle():
    """Every membership and assignment row, without the role columns: ids,
    links, status and all lifecycle metadata."""
    return _rows(MIG._MEMBERSHIP_LIFECYCLE), _rows(MIG._ASSIGNMENT_LIFECYCLE)


def _structure():
    """Columns, foreign keys, unique keys, indexes and CHECK names of the two
    O2 tables -- to prove nothing unrelated is added or dropped."""
    inspector = sa.inspect(db.engine)
    shape = {}
    for table in sorted(O2_TABLES):
        shape[table] = (
            sorted(c["name"] for c in inspector.get_columns(table)),
            sorted((tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                   for fk in inspector.get_foreign_keys(table)),
            sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)),
            sorted((i["name"], tuple(i["column_names"])) for i in inspector.get_indexes(table)),
            sorted(c["name"] for c in inspector.get_check_constraints(table)),
        )
    return shape


def _user_id(username):
    return _scalar("SELECT id FROM users WHERE username = :n", n=username)


def _membership_id(username):
    return _scalar("SELECT m.id FROM organization_memberships m JOIN users u ON u.id = m.user_id "
                   "WHERE u.username = :n", n=username)


def _assignment_id(username):
    return _scalar("SELECT a.id FROM branch_assignments a JOIN organization_memberships m ON m.id = a.membership_id "
                   "JOIN users u ON u.id = m.user_id WHERE u.username = :n", n=username)


def _local_ids():
    return _scalar("SELECT id FROM organizations"), _scalar("SELECT id FROM branches")


def _new_account(username, role, *, active=True, approved=None):
    """An account created after O2's backfill, with no membership (the ORM
    ``User`` model is unchanged by O2/O3.3, so it can write at O2)."""
    user = _account(username, role, active=active, approved=approved)
    db.session.commit()
    user_id = user.id
    _release()
    return user_id


def _insert_membership(user_id, status="active"):
    organization_id, _ = _local_ids()
    revoked_at = REVOKED_AT if status == "revoked" else None
    _exec("INSERT INTO organization_memberships (organization_id, user_id, status, revoked_at, created_at) "
          "VALUES (:o, :u, :s, :r, CURRENT_TIMESTAMP)", o=organization_id, u=user_id, s=status, r=revoked_at)
    return _scalar("SELECT id FROM organization_memberships WHERE user_id = :u", u=user_id)


def _insert_assignment(membership_id, status="active"):
    organization_id, branch_id = _local_ids()
    revoked_at = REVOKED_AT if status == "revoked" else None
    _exec("INSERT INTO branch_assignments (organization_id, membership_id, branch_id, status, revoked_at, "
          "created_at) VALUES (:o, :m, :b, :s, :r, CURRENT_TIMESTAMP)",
          o=organization_id, m=membership_id, b=branch_id, s=status, r=revoked_at)


# O2-era lifecycle changes, written exactly as the O2 services write them (the
# services themselves need the O3.3 models, which cannot run at O2).

def _revoke_membership(username, by):
    membership_id = _membership_id(username)
    _exec("UPDATE branch_assignments SET status = 'revoked', revoked_at = :t, revoked_by_id = :by "
          "WHERE membership_id = :m AND status = 'active'", t=REVOKED_AT, by=by, m=membership_id)
    _exec("UPDATE organization_memberships SET status = 'revoked', revoked_at = :t, revoked_by_id = :by "
          "WHERE id = :m", t=REVOKED_AT, by=by, m=membership_id)


def _revoke_assignment(username, by):
    _exec("UPDATE branch_assignments SET status = 'revoked', revoked_at = :t, revoked_by_id = :by WHERE id = :a",
          t=REVOKED_AT, by=by, a=_assignment_id(username))


def _reinstate_membership(username):
    _exec("UPDATE organization_memberships SET status = 'active', revoked_at = NULL, revoked_by_id = NULL "
          "WHERE id = :m", m=_membership_id(username))


@contextlib.contextmanager
def _foreign_keys_off(commit=True):
    """A connection that does not enforce foreign keys -- how orphan rows get
    in: SQLite without the app's PRAGMA, or MySQL with FOREIGN_KEY_CHECKS=0
    (as a dump restore runs)."""
    with db.engine.connect() as conn:
        mysql = conn.dialect.name == "mysql"
        if mysql:
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))
        else:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            yield conn
            conn.commit() if commit else conn.rollback()
        finally:
            if mysql:
                conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 1"))
            else:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")


@contextlib.contextmanager
def _fail_on(fragment):
    """Make the first statement containing ``fragment`` fail, through the
    engine Flask-Migrate's env.py uses (Alembic loads the revision file on
    its own, so the migration module cannot be patched)."""
    def raise_on(conn, cursor, statement, parameters, context, executemany):
        if fragment in statement:
            raise RuntimeError(f"simulated failure at: {fragment}")
    sa.event.listen(db.engine, "before_cursor_execute", raise_on)
    try:
        yield
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", raise_on)


@contextlib.contextmanager
def _statements(prefix):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(prefix):
            seen.append(" ".join(statement.split()))
    sa.event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", record)


def _small_o2():
    """A minimal installation at O2: one admin and one staff account, each
    with the membership and assignment O2's backfill gives them."""
    upgrade(revision=O1)
    _account("r_admin", Role.ADMIN)
    _account("r_cook", Role.STAFF)
    db.session.commit()
    _release()
    upgrade(revision=O2)


EXTRA_ACCOUNTS = [  # username, role, is_active, staff_approved -- beyond _seed_every_kind_of_account's
    ("mig_admin_ok", Role.ADMIN, True, True),
    ("mig_pending_off", Role.STAFF, False, False),   # pending and inactive
    ("mig_admin_revoked", Role.ADMIN, True, True),
    ("mig_staff_revoked", Role.STAFF, True, True),
    ("mig_staff_asg_revoked", Role.STAFF, True, True),
    ("mig_admin_asg_revoked", Role.ADMIN, True, False),
    ("mig_reinstated", Role.STAFF, True, True),
]


def _populated_o2():
    """A trading installation at O2 with every kind of workforce account
    (O2's own seed: pending admin ``mig_admin``, inactive admin, pending,
    deactivated and invited staff, plus the cashier) and every O2 lifecycle
    variant: revoked memberships (with their assignments), active
    memberships with a revoked assignment, a reinstated membership whose
    assignment stayed revoked, and attributed created_by/revoked_by ids."""
    upgrade(revision=O1)
    _seed_existing_installation()
    _seed_every_kind_of_account()
    for username, role, active, approved in EXTRA_ACCOUNTS:
        _account(username, role, active=active, approved=approved)
    db.session.commit()
    _release()
    upgrade(revision=O2)
    by = _user_id("mig_admin_ok")
    _revoke_membership("mig_admin_revoked", by)
    _revoke_membership("mig_staff_revoked", by)
    _revoke_assignment("mig_staff_asg_revoked", by)
    _revoke_assignment("mig_admin_asg_revoked", by)
    _revoke_membership("mig_reinstated", by)
    _reinstate_membership("mig_reinstated")
    _exec("UPDATE organization_memberships SET created_by_id = :by WHERE id = :m", by=by,
          m=_membership_id("mig_cashier"))


def _roles_by_user():
    return {r[0]: (r[1], r[2]) for r in _rows(
        "SELECT u.username, m.organization_role, a.branch_role FROM organization_memberships m "
        "JOIN users u ON u.id = m.user_id JOIN branch_assignments a ON a.membership_id = m.id")}


def _expected_roles():
    return {r[0]: ("admin", None) if r[1] == "admin" else (None, "staff") for r in _rows(
        "SELECT username, role FROM users WHERE role IN ('admin', 'staff')")}


def _migrate_error(capfd, command, *args, **kwargs) -> str:
    """Run a Flask-Migrate command that must fail, and return what it
    reported. Flask-Migrate logs a migration's RuntimeError and exits 1;
    Alembic's logging config writes that straight to stderr."""
    capfd.readouterr()
    with pytest.raises(SystemExit):
        command(*args, **kwargs)
    return capfd.readouterr().err


def _assert_refused(capfd, *fragments, role_schema=None):
    """The migration's own check, called directly for its message, and then
    through Flask-Migrate: refused, logged, and nothing changed."""
    _release()
    role_schema = role_schema or NO_ROLE_SCHEMA
    tables = _tables()
    before = _snapshot(tables - {"alembic_version"})
    with db.engine.connect() as conn, pytest.raises(MIG.MembershipRoleBackfillRefused) as exc:
        MIG.check_preconditions(conn)
    message = str(exc.value)
    assert "Nothing was changed." in message and MIG.RECOVERY_GUIDE in message
    for fragment in fragments:
        assert fragment in message, message
    reported = _migrate_error(capfd, upgrade)
    assert "O3.3 membership-role migration refused" in reported
    assert all(fragment in reported for fragment in fragments)
    assert _version() == O2
    assert _role_schema() == role_schema
    assert _tables() == tables
    assert _snapshot(tables - {"alembic_version"}) == before
    return message


# --- M1: fresh database ------------------------------------------------------------------------------


def test_fresh_database_gets_the_columns_and_no_rows(scratch):
    upgrade()
    assert _version() == O3
    assert _role_schema() == FULL_ROLE_SCHEMA
    assert _scalar("SELECT COUNT(*) FROM organization_memberships") == 0
    assert _scalar("SELECT COUNT(*) FROM branch_assignments") == 0
    assert org.check_consistency() == []


# --- M2/M3: existing populated installation ------------------------------------------------------------


def test_existing_installation_backfill_changes_nothing_but_the_roles(scratch):
    _populated_o2()
    tables = _tables() - {"alembic_version"}
    others = tables - O2_TABLES
    before_others = _snapshot(others)        # users, audit log, orders, ledger, identity, ...
    lifecycle_before = _lifecycle()
    ledger_before = _ledger_audit()
    structure_before = _structure()
    sqlite = db.engine.dialect.name == "sqlite"
    rootpages_before = _rows("SELECT name, rootpage FROM sqlite_master WHERE type = 'table'") if sqlite else None
    expected = _expected_roles()
    assert len(expected) == 6 + len(EXTRA_ACCOUNTS)  # cashier + O2's five + the extras

    upgrade()

    assert _version() == O3
    assert _role_schema() == FULL_ROLE_SCHEMA
    # Exactly the role each account implies, on rows of every status.
    assert _roles_by_user() == expected
    assert _roles_by_user()["mig_admin"] == ("admin", None)             # pending ADMIN
    assert _roles_by_user()["mig_admin_off"] == ("admin", None)         # inactive ADMIN
    assert _roles_by_user()["mig_pending"] == (None, "staff")           # pending STAFF
    assert _roles_by_user()["mig_invited"] == (None, "staff")           # invited STAFF
    assert _roles_by_user()["mig_pending_off"] == (None, "staff")
    assert _roles_by_user()["mig_staff_revoked"] == (None, "staff")     # revoked rows keep a role ...
    assert _rows("SELECT m.status, a.status FROM organization_memberships m JOIN branch_assignments a "
                 "ON a.membership_id = m.id WHERE m.id = :m", m=_membership_id("mig_staff_revoked")) \
        == [("revoked", "revoked")]                                     # ... and stay revoked
    assert _scalar("SELECT COUNT(*) FROM organization_memberships WHERE organization_role = 'owner'") == 0
    assert _scalar("SELECT COUNT(*) FROM branch_assignments WHERE branch_role = 'manager'") == 0
    # No row inserted, deleted or reactivated; ids and lifecycle identical.
    assert _lifecycle() == lifecycle_before
    # Everything else identical: accounts, audit log (no audit row written), trading data.
    assert _snapshot(others) == before_others
    assert _ledger_audit() == ledger_before
    # Only the two role columns and their CHECKs were added.
    structure_after = _structure()
    assert structure_after["organization_memberships"][0] == sorted(
        structure_before["organization_memberships"][0] + ["organization_role"])
    assert structure_after["branch_assignments"][0] == sorted(
        structure_before["branch_assignments"][0] + ["branch_role"])
    for table, check in (("organization_memberships", "ck_organization_memberships_organization_role"),
                         ("branch_assignments", "ck_branch_assignments_branch_role")):
        assert structure_after[table][1:4] == structure_before[table][1:4]
        assert structure_after[table][4] == sorted(structure_before[table][4] + [check])
    if sqlite:  # M3: no table was rebuilt, and every foreign key still holds
        assert _rows("SELECT name, rootpage FROM sqlite_master WHERE type = 'table'") == rootpages_before
        assert _rows("PRAGMA foreign_key_check") == []
    # Eligibility is unchanged and follows the approved policy.
    for user in db.session.execute(sa.select(User).where(User.role.in_([Role.ADMIN, Role.STAFF]))).scalars():
        policy = user.is_active and (user.role == Role.ADMIN or user.staff_approved)
        assert auth_context_for_user(user).is_eligible is policy, user.username
    eligible = {u.username: auth_context_for_user(u).is_eligible
                for u in db.session.execute(sa.select(User)).scalars()}
    assert (eligible["mig_admin"], eligible["mig_pending"], eligible["mig_admin_off"], eligible["mig_invited"]) \
        == (True, False, False, False)
    assert org.check_consistency() == []


# --- M4-M13: refusals before any DDL -------------------------------------------------------------------


@pytest.mark.parametrize("extra", ["branch", "organization"])
def test_r1_unexpected_identity(scratch, capfd, extra):
    _small_o2()
    organization_id, _ = _local_ids()
    if extra == "organization":
        _exec("INSERT INTO organizations (public_id, name) VALUES ('0b1c2d3e-0000-4000-8000-0000000000a1', 'Second')")
    else:
        _exec("INSERT INTO branches (organization_id, public_id, name) VALUES "
              "(:o, '0b1c2d3e-0000-4000-8000-0000000000a2', 'Second')", o=organization_id)
    _assert_refused(capfd, "R1 installation identity")


@pytest.mark.parametrize("username, role, active, approved", [
    ("lone_admin", Role.ADMIN, True, False),        # an admin as create-admin makes one
    ("lone_staff", Role.STAFF, True, True),
    ("lone_pending", Role.STAFF, True, False),      # pending is not exempt
    ("lone_invited", Role.STAFF, False, True),      # nor invited
])
def test_r2_workforce_account_without_membership(scratch, capfd, username, role, active, approved):
    _small_o2()
    user_id = _new_account(username, role, active=active, approved=approved)
    _assert_refused(capfd, f"R2 account without membership: user {user_id} '{username}' ({role.value}",
                    "m = add_member(user, actor=a); assign_branch(m, branch, actor=a)")


@pytest.mark.parametrize("status", ["active", "revoked"])
def test_r3_customer_membership(scratch, capfd, status):
    _small_o2()
    user_id = _new_account("diner", Role.CUSTOMER)
    membership_id = _insert_membership(user_id, status)
    _insert_assignment(membership_id, status)
    _assert_refused(capfd, f"R3 membership {membership_id} ({status}) belongs to a customer account: "
                            f"user {user_id} 'diner' (customer)", "restore a verified backup")


def test_r3_membership_of_a_deleted_user(scratch, capfd):
    _small_o2()
    user_id, membership_id = _user_id("r_cook"), _membership_id("r_cook")
    with _foreign_keys_off() as conn:
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": user_id})
    _assert_refused(capfd, f"R3 membership {membership_id} (active) references a missing user: "
                            f"user {user_id} <missing user>")


def test_r4_membership_in_a_missing_organization(scratch, capfd):
    _small_o2()
    user_id, membership_id = _user_id("r_cook"), _membership_id("r_cook")
    with _foreign_keys_off() as conn:
        conn.execute(sa.text("UPDATE organization_memberships SET organization_id = 999 WHERE id = :m"),
                     {"m": membership_id})
    _assert_refused(capfd, f"R4 membership {membership_id} (user {user_id}) is in organization 999")


def test_r5_orphan_assignment(scratch, capfd):
    _small_o2()
    membership_id, assignment_id = _membership_id("r_cook"), _assignment_id("r_cook")
    with _foreign_keys_off() as conn:
        conn.execute(sa.text("DELETE FROM organization_memberships WHERE id = :m"), {"m": membership_id})
    message = _assert_refused(capfd, f"R5 orphan assignment {assignment_id} (active): membership {membership_id} "
                                      "does not exist")
    assert f"R2 account without membership: user {_user_id('r_cook')} 'r_cook'" in message  # both reported


@pytest.mark.parametrize("change, fragment", [
    ("organization_id = 999", "organization 999, membership's organization"),
    ("branch_id = 999", "branch 999 (missing)"),
])
def test_r6_assignment_not_matching_its_membership_or_branch(scratch, capfd, change, fragment):
    _small_o2()
    assignment_id = _assignment_id("r_cook")
    with _foreign_keys_off() as conn:
        conn.execute(sa.text(f"UPDATE branch_assignments SET {change} WHERE id = :a"), {"a": assignment_id})
    _assert_refused(capfd, f"R6 assignment {assignment_id} (membership {_membership_id('r_cook')})", fragment)


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
@pytest.mark.parametrize("status, fix", [
    ("active", "Fix (previous release): assign_branch(m, branch, actor=a)."),
    ("revoked", "reinstate_membership(m, actor=a); assign_branch(m, branch, actor=a); revoke_membership(m, actor=a)"),
])
def test_r7_membership_without_an_assignment_row(scratch, capfd, role, status, fix):
    """D5/D6: any role, any status -- every creation path writes both rows."""
    _small_o2()
    user_id = _new_account("lonely", role)
    membership_id = _insert_membership(user_id, status)
    _assert_refused(capfd, f"R7 membership {membership_id} ({status}) of user {user_id} 'lonely' ({role.value}",
                    "has no branch assignment row", fix)


def test_r8_active_assignment_under_a_revoked_membership(scratch, capfd):
    _small_o2()
    membership_id, assignment_id = _membership_id("r_cook"), _assignment_id("r_cook")
    _exec("UPDATE organization_memberships SET status = 'revoked', revoked_at = :t WHERE id = :m",
          t=REVOKED_AT, m=membership_id)
    _assert_refused(capfd, f"R8 active assignment {assignment_id} under revoked membership {membership_id} "
                            f"(user {_user_id('r_cook')} 'r_cook')", "revoke_assignment(assignment, actor=a)")


@pytest.mark.parametrize("which", [0, 1])
def test_r9_role_column_already_present(scratch, capfd, which):
    _small_o2()
    rc = MIG.ROLE_COLUMNS[which]
    with db.engine.begin() as conn:
        MIG.add_role_column(conn, rc)
    schema = dict(NO_ROLE_SCHEMA, **{rc.column: (True, True)})
    _assert_refused(capfd, f"R9 {rc.table}.{rc.column}: column and CHECK constraint already present",
                    "This migration never drops it", role_schema=schema)


def test_every_problem_is_reported_at_once(scratch, capfd):
    _small_o2()
    lone_id = _new_account("lone_admin", Role.ADMIN)
    diner_id = _new_account("diner", Role.CUSTOMER)
    diner_membership = _insert_membership(diner_id)
    _insert_assignment(diner_membership)
    admin_membership = _membership_id("r_admin")
    _exec("UPDATE organization_memberships SET status = 'revoked', revoked_at = :t WHERE id = :m",
          t=REVOKED_AT, m=admin_membership)
    cook_membership, cook_assignment = _membership_id("r_cook"), _assignment_id("r_cook")
    with _foreign_keys_off() as conn:
        conn.execute(sa.text("DELETE FROM organization_memberships WHERE id = :m"), {"m": cook_membership})
    message = _assert_refused(capfd, f"user {lone_id} 'lone_admin'", f"R3 membership {diner_membership}",
                              f"R5 orphan assignment {cook_assignment}",
                              f"under revoked membership {admin_membership}")
    assert "5 problem(s)" in message  # R2 x2 (lone_admin, r_cook), R3, R5, R8


# --- M14: the documented repairs, with the O2 service calls and their actor ----------------------------
#
# Run on the O3.3 code (a head-schema database): the repairs are the same O2
# service functions the previous release ships, but that release itself is not
# exercised here. Each repaired state must pass every pre-DDL check (R9 aside,
# as the head schema has the columns) and check_consistency().


def _problems_except_r9():
    with db.engine.connect() as conn:
        return [p for p in MIG.find_problems(conn) if not p.startswith("R9")]


@pytest.fixture
def repair_world(installation_identity):
    _, branch = installation_identity
    actor = make_user(username="fixer", email="fixer@example.com", role=Role.ADMIN, staff_approved=False)
    membership = org.add_member(actor)
    org.assign_branch(membership, branch)
    assert _problems_except_r9() == []
    return actor, branch


def _audit_actors(event):
    return [r[0] for r in db.session.execute(sa.text(
        "SELECT user_id FROM audit_logs WHERE event_type = :e"), {"e": event})]


def test_repair_r2_add_member_then_assign_branch(repair_world):
    actor, branch = repair_world
    cook = make_user(username="cook", email="cook@example.com", role=Role.STAFF, staff_approved=False)
    assert any(p.startswith("R2") for p in _problems_except_r9())
    membership = org.add_member(cook, actor=actor)
    org.assign_branch(membership, branch, actor=actor)
    assert _problems_except_r9() == [] and org.check_consistency() == []
    assert _audit_actors("membership_created")[-1] == actor.id
    assert _audit_actors("branch_assignment_created")[-1] == actor.id


def test_repair_r7_active_membership(repair_world):
    actor, branch = repair_world
    cook = make_user(username="cook", email="cook@example.com", role=Role.STAFF)
    membership = org.add_member(cook, actor=actor)
    assert any(p.startswith("R7") for p in _problems_except_r9())
    org.assign_branch(membership, branch, actor=actor)
    assert _problems_except_r9() == [] and org.check_consistency() == []


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_repair_r7_revoked_membership(repair_world, role):
    """reinstate -> assign -> revoke, each with the actor: ends revoked, with a
    revoked assignment row; the membership's revoked_at/revoked_by_id become
    the repair's (the audit log keeps both revocations)."""
    actor, branch = repair_world
    user = make_user(username="lonely", email="lonely@example.com", role=role)
    membership = org.add_member(user, actor=actor)
    org.revoke_membership(membership, actor=actor)
    assert any(p.startswith("R7") and "(revoked)" in p for p in _problems_except_r9())

    assert org.reinstate_membership(membership, actor=actor) is True
    org.assign_branch(membership, branch, actor=actor)
    assert org.revoke_membership(membership, actor=actor) is True

    db.session.rollback()
    row = db.session.get(OrganizationMembership, membership.id)
    assignments = db.session.execute(sa.select(BranchAssignment).filter_by(membership_id=membership.id)).scalars().all()
    assert (row.status.value, row.revoked_by_id) == ("revoked", actor.id)
    assert [(a.status.value, a.revoked_by_id) for a in assignments] == [("revoked", actor.id)]
    assert _audit_actors("membership_revoked")[-2:] == [actor.id, actor.id]
    assert _audit_actors("membership_reinstated")[-1] == actor.id
    assert _problems_except_r9() == [] and org.check_consistency() == []


def test_repair_r8_revoke_assignment(repair_world):
    actor, branch = repair_world
    cook = make_user(username="cook", email="cook@example.com", role=Role.STAFF)
    membership = org.add_member(cook, actor=actor)
    assignment = org.assign_branch(membership, branch, actor=actor)
    db.session.execute(sa.text("UPDATE organization_memberships SET status = 'revoked', revoked_at = :t "
                               "WHERE id = :m"), {"t": REVOKED_AT, "m": membership.id})
    db.session.commit()
    assert any(p.startswith("R8") for p in _problems_except_r9())
    assert org.revoke_assignment(assignment, actor=actor) is True
    assert _problems_except_r9() == [] and org.check_consistency() == []
    assert _audit_actors("branch_assignment_revoked")[-1] == actor.id


# --- the backfill's own verification -------------------------------------------------------------------


def test_backfill_verification_catches_wrong_roles_changed_rows_and_orphans(scratch):
    _small_o2()
    upgrade()
    before = _lifecycle()
    admin_membership = _membership_id("r_admin")
    with db.engine.connect() as conn:
        MIG.verify_backfill(conn, *before)  # the real result passes
        conn.execute(sa.text("UPDATE organization_memberships SET organization_role = NULL WHERE id = :m"),
                     {"m": admin_membership})
        with pytest.raises(MIG.MembershipRoleBackfillFailed, match=r"1 membership\(s\) without the expected"):
            MIG.verify_backfill(conn, *before)
        conn.rollback()
        with pytest.raises(MIG.MembershipRoleBackfillFailed, match="membership rows, ids or lifecycle values changed"):
            MIG.verify_backfill(conn, before[0][:-1], before[1])
        conn.rollback()
    # An assignment whose membership is gone counts as wrong; it cannot drop out of the join.
    with _foreign_keys_off(commit=False) as conn:
        conn.execute(sa.text("DELETE FROM organization_memberships WHERE id = :m"),
                     {"m": _membership_id("r_cook")})
        with pytest.raises(MIG.MembershipRoleBackfillFailed, match=r"1 assignment\(s\) without the expected"):
            MIG.verify_backfill(conn, before[0], before[1])
    assert _lifecycle() == before  # rolled back


# --- DDL shape: one ALTER TABLE per table each way, nothing unrelated touched --------------------------


def test_one_alter_table_per_table_each_way_and_nothing_else_changes(scratch):
    _small_o2()
    structure_o2 = _structure()
    with _statements("ALTER TABLE") as ups:
        upgrade()
    mysql = db.engine.dialect.name == "mysql"
    assert len(ups) == 2
    assert ups[0].startswith("ALTER TABLE organization_memberships ADD COLUMN organization_role VARCHAR(16)")
    assert ups[1].startswith("ALTER TABLE branch_assignments ADD COLUMN branch_role VARCHAR(16)")
    assert all(("ADD CONSTRAINT ck_" if mysql else "CONSTRAINT ck_") in s for s in ups)
    _release()
    with _statements("ALTER TABLE") as downs:
        downgrade(revision=O2)
    if mysql:
        assert downs == ["ALTER TABLE branch_assignments DROP CHECK ck_branch_assignments_branch_role, "
                         "DROP COLUMN branch_role",
                         "ALTER TABLE organization_memberships DROP CHECK ck_organization_memberships_organization_role, "
                         "DROP COLUMN organization_role"]
    else:
        assert downs == ["ALTER TABLE branch_assignments DROP COLUMN branch_role",
                         "ALTER TABLE organization_memberships DROP COLUMN organization_role"]
    assert _structure() == structure_o2   # no unrelated column, key, index or CHECK dropped
    assert _version() == O2


# --- M15-M17: downgrade -------------------------------------------------------------------------------


def test_downgrade_and_reupgrade_recompute_identical_roles(scratch):
    _small_o2()
    _revoke_membership("r_cook", by=_user_id("r_admin"))
    upgrade()
    create_admin(RegistrationInput(username="later", email="later@example.com", password=PASSWORD,
                                   password_confirm=PASSWORD))   # enrolled at head, with its role
    _release()
    full_rows = (_rows("SELECT * FROM organization_memberships ORDER BY id"),
                 _rows("SELECT * FROM branch_assignments ORDER BY id"))
    lifecycle = _lifecycle()
    others = _tables() - O2_TABLES - {"alembic_version"}
    others_before = _snapshot(others)

    downgrade(revision=O2)
    assert _version() == O2 and _role_schema() == NO_ROLE_SCHEMA
    assert _lifecycle() == lifecycle            # revoked stays revoked; nothing else changes
    assert _snapshot(others) == others_before

    upgrade()
    assert _version() == O3
    assert (_rows("SELECT * FROM organization_memberships ORDER BY id"),
            _rows("SELECT * FROM branch_assignments ORDER BY id")) == full_rows
    assert _roles_by_user()["later"] == ("admin", None)


@pytest.mark.parametrize("table, column, value", [
    ("organization_memberships", "organization_role", "owner"),
    ("branch_assignments", "branch_role", "manager"),
])
def test_downgrade_refuses_a_role_it_could_not_recompute(scratch, capfd, table, column, value):
    _small_o2()
    upgrade()
    _exec(f"UPDATE {table} SET {column} = :v WHERE id = (SELECT MIN(id) FROM (SELECT id FROM {table}) t)", v=value)
    assert "O3.3 downgrade refused, nothing dropped" in _migrate_error(capfd, downgrade, revision=O2)
    assert _version() == O3 and _role_schema() == FULL_ROLE_SCHEMA
    assert _scalar(f"SELECT COUNT(*) FROM {table} WHERE {column} = :v", v=value) == 1


def test_interrupted_downgrade_can_be_run_again(scratch):
    _small_o2()
    upgrade()
    lifecycle = _lifecycle()
    with db.engine.begin() as conn:      # as if the second ALTER of a downgrade had failed
        MIG.drop_role_column(conn, MIG.BRANCH_ROLE)
    assert _version() == O3
    assert _role_schema() == {"organization_role": (True, True), "branch_role": (False, False)}
    downgrade(revision=O2)               # skips the finished table, completes the other
    assert _version() == O2 and _role_schema() == NO_ROLE_SCHEMA
    assert _lifecycle() == lifecycle


# --- M18/M19: a failure part-way, recovery and retry --------------------------------------------------


def _backup(path):
    """The pre-upgrade backup: a file copy on SQLite (app stopped); on MySQL
    the guarded procedure is exercised instead (no mysqldump in tests)."""
    if db.engine.dialect.name == "sqlite":
        db.session.remove()
        db.engine.dispose()
        shutil.copyfile(path, path.with_suffix(".backup"))


def _recover(path):
    """The documented recovery for state X or Y. SQLite: restore the file
    copy. MySQL: the guarded procedure -- version still O2, every role value
    NULL, column and CHECK in step -- then one ALTER per present column."""
    if db.engine.dialect.name == "sqlite":
        db.session.remove()
        db.engine.dispose()
        shutil.copyfile(path.with_suffix(".backup"), path)
        return
    assert _version() == O2
    with db.engine.begin() as conn:
        for rc in reversed(MIG.ROLE_COLUMNS):
            present = MIG.column_present(conn, rc)
            assert present == MIG.check_present(conn, rc)
            if present:
                assert conn.execute(sa.text(
                    f"SELECT COUNT(*) FROM {rc.table} WHERE {rc.column} IS NOT NULL")).scalar() == 0
                MIG.drop_role_column(conn, rc)


@pytest.mark.parametrize("fail_at, state", [
    ("ALTER TABLE branch_assignments ADD COLUMN", STATE_X),             # M18: DDL-2 fails
    ("WHERE CASE WHEN (u.role = 'admin'", FULL_ROLE_SCHEMA),            # M19: the verification fails (state Y)
])
def test_partial_failure_leaves_a_recoverable_state(scratch, capfd, fail_at, state):
    _small_o2()
    _revoke_assignment("r_cook", by=_user_id("r_admin"))
    _backup(scratch)
    lifecycle = _lifecycle()
    others = _tables() - O2_TABLES - {"alembic_version"}
    others_before = _snapshot(others)
    structure_o2 = _structure()

    with _fail_on(fail_at):
        assert "simulated failure" in _migrate_error(capfd, upgrade)

    # The state actually left behind, through the real env.py: the committed
    # ALTER(s) remain, the backfill and the version stamp do not.
    assert _version() == O2
    assert _role_schema() == state
    for rc in MIG.ROLE_COLUMNS:
        if state[rc.column][0]:
            assert _scalar(f"SELECT COUNT(*) FROM {rc.table} WHERE {rc.column} IS NOT NULL") == 0
    assert _lifecycle() == lifecycle and _snapshot(others) == others_before

    # A plain retry is refused (R9) and drops nothing.
    assert "R9 organization_memberships.organization_role: column and CHECK constraint already present" \
        in _migrate_error(capfd, upgrade)
    assert _role_schema() == state and _version() == O2

    # Recovery returns the exact O2 schema and data; the retry then succeeds.
    _recover(scratch)
    assert _version() == O2 and _role_schema() == NO_ROLE_SCHEMA
    assert _structure() == structure_o2
    assert _lifecycle() == lifecycle and _snapshot(others) == others_before
    upgrade()
    assert _version() == O3 and _roles_by_user() == _expected_roles()
    assert _lifecycle() == lifecycle


# --- MySQL only (M20/M21) -------------------------------------------------------------------------------


def _mysql_only():
    if not MYSQL_URL:
        pytest.skip("needs TEST_DATABASE_URL=mysql+pymysql://...*_test (CI's O3.3 guard requires it to run)")


def _mysql_checks():
    """The two O3.3 CHECKs, by exact name (a LIKE pattern would also match
    the unrelated ``ck_users_role``)."""
    return _rows("SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                 "WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK' "
                 "AND CONSTRAINT_NAME IN ('ck_organization_memberships_organization_role', "
                 "'ck_branch_assignments_branch_role') ORDER BY CONSTRAINT_NAME")


def test_mysql_role_checks_are_enforced_and_removed_atomically(scratch):
    _mysql_only()
    _small_o2()
    constraints_o2 = _rows("SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE FROM information_schema."
                           "TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE() ORDER BY 1, 2")
    upgrade()
    assert db.engine.dialect.name == "mysql"
    assert _mysql_checks() == [("branch_assignments", "ck_branch_assignments_branch_role"),
                               ("organization_memberships", "ck_organization_memberships_organization_role")]
    with db.engine.connect() as conn:
        for sql in ("UPDATE organization_memberships SET organization_role = 'manager'",
                    "UPDATE organization_memberships SET organization_role = 'Admin'",
                    "UPDATE branch_assignments SET branch_role = 'owner'",
                    "UPDATE branch_assignments SET branch_role = ''"):
            with rejected_by_check_constraint():
                conn.execute(sa.text(sql))
            conn.rollback()
    downgrade(revision=O2)
    assert _mysql_checks() == []
    assert _rows("SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE FROM information_schema.TABLE_CONSTRAINTS "
                 "WHERE CONSTRAINT_SCHEMA = DATABASE() ORDER BY 1, 2") == constraints_o2


def test_mysql_leftover_check_name_elsewhere_is_refused(scratch, capfd):
    """MySQL scopes CHECK names to the schema, so a leftover on any table
    would break the ADD CONSTRAINT; R9 refuses first."""
    _mysql_only()
    _small_o2()
    _exec("ALTER TABLE organizations ADD CONSTRAINT ck_branch_assignments_branch_role CHECK (id > 0)")
    _assert_refused(capfd, "R9 branch_assignments.branch_role: CHECK constraint already present")


def test_mysql_downgrade_refuses_a_column_without_its_check(scratch, capfd):
    _mysql_only()
    _small_o2()
    upgrade()
    _exec("ALTER TABLE branch_assignments DROP CHECK ck_branch_assignments_branch_role")
    assert "column branch_role present but CHECK ck_branch_assignments_branch_role absent" \
        in _migrate_error(capfd, downgrade, revision=O2)
    assert _version() == O3
    assert _role_schema() == {"organization_role": (True, True), "branch_role": (True, False)}
