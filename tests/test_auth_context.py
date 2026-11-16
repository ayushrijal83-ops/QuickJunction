"""O3.4: the membership-aware authorization context
(app/services/auth_context.py).

Resolution only: nothing here changes who may do what. The resolved context
must reflect the database as it is -- the local organization and branch, the
account's membership and assignment and their stored roles -- and must fail
closed (``WorkforceContextError``) for an eligible workforce account whose
organization context cannot be trusted. Customers and ineligible accounts
keep the global-only context without any workforce lookup. The resolver is
read-only: it never flushes, writes, commits or rolls back the caller's
session.

States the database's own constraints make impossible (a second membership
for one account, a second assignment to the one branch) are tested through
the pure rule functions with synthetic rows.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from flask import g

from app.extensions import db as _db
from app.models.audit_log import AuditLog
from app.models.organization import BranchAssignment, BranchRole, OrganizationMembership, OrganizationRole
from app.models.user import Role, User
from app.services import auth_context as ac
from app.services import organization as org
from app.services.auth_context import (
    WorkforceContextError, current_resolved_auth_context, local_organization_and_branch_ids,
    resolve_auth_context, workforce_context)
from app.utils.permissions import (
    BRANCH_ROLE_GRANTS, COMPATIBILITY_GRANTS, ORGANIZATION_ROLE_GRANTS, AuthContext, auth_context_for_user,
    current_auth_context, permissions_for)
from app.utils.security import hash_password
from tests.conftest import make_user

PASSWORD = "correct-horse-1"


# --- helpers -----------------------------------------------------------------------------------------


@pytest.fixture
def local(installation_identity):
    organization, branch = installation_identity
    return SimpleNamespace(organization_id=organization.id, branch_id=branch.id, organization=organization,
                           branch=branch)


def _account(name, role, *, active=True, approved=None):
    return make_user(username=name, email=f"{name}@example.com", password=PASSWORD, role=role, is_active=active,
                     staff_approved=approved)


def _enrolled(local, name, role, **kwargs):
    """An account enrolled exactly as the account-creation paths do."""
    user = _account(name, role, **kwargs)
    org.enroll_new_account(user, local.organization, local.branch)
    _db.session.commit()
    return user


def _membership(user):
    return _db.session.execute(sa.select(OrganizationMembership).filter_by(user_id=user.id)).scalar_one()


def _assignment(user):
    return _db.session.execute(sa.select(BranchAssignment).filter_by(membership_id=_membership(user).id)).scalar_one()


def _set(sql, **params):
    _db.session.execute(sa.text(sql), params)
    _db.session.commit()


# (turn off, turn on, read back) -- both settings are per connection.
_FOREIGN_KEY_SQL = {
    "mysql": ("SET FOREIGN_KEY_CHECKS = 0", "SET FOREIGN_KEY_CHECKS = 1", "SELECT @@foreign_key_checks"),
    "sqlite": ("PRAGMA foreign_keys=OFF", "PRAGMA foreign_keys=ON", "PRAGMA foreign_keys"),
}


@contextlib.contextmanager
def _foreign_keys_off():
    """A connection that does not enforce foreign keys, for states only raw
    SQL can create past the composite foreign keys. Yields that connection;
    write through it, not the session.

    One ``Connection`` keeps one physical connection from checkout to close,
    so checks are turned back on on the very connection they were turned off
    on. (Going through the session instead is unsafe: ``Session.commit()``
    returns its connection to the pool, and the next ``session.connection()``
    may check out a different one under MySQL's ``QueuePool``, leaving the
    first pooled with checks off.) Restoring runs outside any transaction,
    where SQLite honours the PRAGMA, and is read back; if it cannot be shown
    restored, the connection is invalidated so the pool discards it."""
    _db.session.commit()                      # the session holds no connection or transaction
    off, on, probe = _FOREIGN_KEY_SQL[_db.engine.dialect.name]
    restored = False
    with _db.engine.connect() as connection:
        physical = connection.connection.dbapi_connection
        connection.exec_driver_sql(off)
        try:
            yield connection
            connection.commit()
        finally:
            try:
                connection.rollback()         # no-op after the commit; ends a failed body's transaction
                connection.exec_driver_sql(on)
                restored = (connection.connection.dbapi_connection is physical
                            and connection.exec_driver_sql(probe).scalar() == 1)
                connection.commit()
            except Exception:
                restored = False
            if not restored:
                connection.invalidate()       # never back into the pool with checks off
    assert restored, "foreign-key checks could not be restored; the connection was discarded"


def _refused(user, code):
    with pytest.raises(WorkforceContextError) as exc:
        resolve_auth_context(user)
    assert exc.value.code == code, (exc.value.code, str(exc.value))
    return exc.value


@contextlib.contextmanager
def _statements():
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    sa.event.listen(_db.engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa.event.remove(_db.engine, "before_cursor_execute", record)


def _snapshot():
    """Every membership, assignment and audit row -- to prove resolution writes nothing."""
    _db.session.expire_all()
    return (_db.session.execute(sa.text("SELECT * FROM organization_memberships ORDER BY id")).all(),
            _db.session.execute(sa.text("SELECT * FROM branch_assignments ORDER BY id")).all(),
            _db.session.scalar(sa.select(sa.func.count()).select_from(AuditLog)))


# --- context construction ------------------------------------------------------------------------------


@pytest.mark.parametrize("approved", [True, False], ids=["approved", "pending"])
def test_active_admin_resolves_to_the_local_organization_whatever_staff_approved_says(local, approved):
    admin = _enrolled(local, "boss", Role.ADMIN, approved=approved)
    context = resolve_auth_context(admin)
    assert context == AuthContext(user_id=admin.id, global_role=Role.ADMIN, is_active=True, staff_approved=approved,
                                  organization_id=local.organization_id, branch_id=local.branch_id,
                                  organization_role=OrganizationRole.ADMIN, branch_role=None)
    assert context.is_eligible
    assert permissions_for(context) == COMPATIBILITY_GRANTS[Role.ADMIN]   # roles add nothing yet


def test_approved_active_staff_resolves_with_the_staff_branch_role(local):
    cook = _enrolled(local, "cook", Role.STAFF)
    context = resolve_auth_context(cook)
    assert context == AuthContext(user_id=cook.id, global_role=Role.STAFF, is_active=True, staff_approved=True,
                                  organization_id=local.organization_id, branch_id=local.branch_id,
                                  organization_role=None, branch_role=BranchRole.STAFF)
    assert permissions_for(context) == COMPATIBILITY_GRANTS[Role.STAFF]


@pytest.mark.parametrize("role, active, approved", [
    (Role.STAFF, True, False),     # pending STAFF
    (Role.STAFF, False, True),     # inactive STAFF
    (Role.ADMIN, False, True),     # inactive ADMIN
    (Role.ADMIN, False, False),
], ids=["pending_staff", "inactive_staff", "inactive_admin", "inactive_pending_admin"])
def test_ineligible_accounts_keep_the_global_only_context_without_any_lookup(local, role, active, approved):
    user = _enrolled(local, "member1", role, active=active, approved=approved)
    _set("DELETE FROM branch_assignments")       # even broken membership data is not looked at
    _set("DELETE FROM organization_memberships")
    _db.session.refresh(user)                     # the caller's row is loaded; count only the resolver
    with _statements() as seen:
        context = resolve_auth_context(user)
    assert seen == []
    assert context == auth_context_for_user(user)
    assert context.organization_id is None and context.organization_role is None and context.branch_role is None
    assert permissions_for(context) == frozenset()


def test_customers_are_never_resolved_into_a_workforce_context(local):
    diner = _account("diner", Role.CUSTOMER)
    # Even with a (raw-SQL) membership row, a customer is not looked up.
    _set("INSERT INTO organization_memberships (organization_id, user_id, status, organization_role, created_at) "
         "VALUES (:o, :u, 'active', 'admin', CURRENT_TIMESTAMP)", o=local.organization_id, u=diner.id)
    _db.session.refresh(diner)
    with _statements() as seen:
        context = resolve_auth_context(diner)
    assert seen == []
    assert context == auth_context_for_user(diner)
    assert context.organization_id is None and permissions_for(context) == frozenset()


# --- membership and assignment lifecycle -------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_missing_membership_fails_closed(local, role):
    _refused(_account("lonely", role), "missing_membership")


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_revoked_membership_fails_closed_and_reinstating_resolves_again(local, role):
    user = _enrolled(local, "member1", role)
    membership, assignment = _membership(user), _assignment(user)
    org.revoke_membership(membership)                    # revokes its assignment too
    _refused(user, "revoked_membership")
    org.reinstate_membership(membership)                 # restores no assignment
    _refused(user, "revoked_assignment")
    org.reinstate_assignment(assignment)
    assert resolve_auth_context(user).organization_id == local.organization_id


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_missing_assignment_fails_closed(local, role):
    user = _account("member1", role)
    org.add_member(user)                                 # membership only
    _refused(user, "missing_assignment")


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_revoked_assignment_fails_closed(local, role):
    user = _enrolled(local, "member1", role)
    org.revoke_assignment(_assignment(user))
    _refused(user, "revoked_assignment")


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_a_stored_owner_role_is_rejected(local, role):
    user = _enrolled(local, "member1", role)
    _set("UPDATE organization_memberships SET organization_role = 'owner' WHERE user_id = :u", u=user.id)
    _refused(user, "unexpected_owner")


@pytest.mark.parametrize("role, organization_role, branch_role", [
    (Role.ADMIN, None, None),            # ADMIN without its organization role
    (Role.ADMIN, "admin", "manager"),    # ADMIN given an invented branch-manager role
    (Role.ADMIN, "admin", "staff"),      # ADMIN given the staff branch role
    (Role.STAFF, "admin", "staff"),      # STAFF given the organization admin role
    (Role.STAFF, None, "manager"),       # STAFF given the manager branch role
    (Role.STAFF, None, None),            # STAFF without its branch role
])
def test_a_stored_role_that_does_not_match_the_account_fails_closed(local, role, organization_role, branch_role):
    user = _enrolled(local, "member1", role)
    _set("UPDATE organization_memberships SET organization_role = :r WHERE user_id = :u", r=organization_role,
         u=user.id)
    _set("UPDATE branch_assignments SET branch_role = :r", r=branch_role)
    _refused(user, "role_mismatch")


# --- integrity and isolation ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_a_membership_in_another_organization_fails_closed(local, role):
    user = _enrolled(local, "member1", role)
    with _foreign_keys_off() as connection:
        connection.execute(sa.text("UPDATE organization_memberships SET organization_id = 999 WHERE user_id = :u"),
                           {"u": user.id})
    _refused(user, "wrong_organization")


@pytest.mark.parametrize("role", [Role.ADMIN, Role.STAFF])
def test_an_assignment_to_another_branch_fails_closed(local, role):
    user = _enrolled(local, "member1", role)
    with _foreign_keys_off() as connection:
        connection.execute(sa.text("UPDATE branch_assignments SET branch_id = 999"))
    _refused(user, "wrong_branch")


def test_an_assignment_in_another_organization_fails_closed(local):
    user = _enrolled(local, "member1", Role.STAFF)
    with _foreign_keys_off() as connection:
        connection.execute(sa.text("UPDATE branch_assignments SET organization_id = 999"))
    _refused(user, "wrong_organization")


def _checks_on(physical):
    """Read the setting straight off one physical connection."""
    cursor = physical.cursor()
    try:
        cursor.execute(_FOREIGN_KEY_SQL[_db.engine.dialect.name][2])
        return cursor.fetchone()[0] == 1
    finally:
        cursor.close()
        physical.rollback()                   # leave no implicit transaction open on it


_ORPHAN_MEMBERSHIP = ("INSERT INTO organization_memberships (organization_id, user_id, status, created_at) "
                      "VALUES (999, :u, 'active', CURRENT_TIMESTAMP)")
_ORPHANS = "SELECT COUNT(*) FROM organization_memberships WHERE organization_id = 999"


def test_the_foreign_key_helper_restores_checks_after_a_successful_body(local):
    """Checks are off only inside the helper, come back on on that same
    physical connection, and the database enforces foreign keys again."""
    user = _account("member1", Role.STAFF)
    with _foreign_keys_off() as connection:
        physical = connection.connection.dbapi_connection
        assert not _checks_on(physical)
    assert _checks_on(physical)
    with pytest.raises(sa.exc.IntegrityError, match=r"(?i)foreign key"):
        _set(_ORPHAN_MEMBERSHIP, u=user.id)
    _db.session.rollback()


def test_the_foreign_key_helper_rolls_back_and_restores_checks_when_the_body_raises(local):
    """The body writes, so a transaction is open when it raises -- the case
    where SQLite ignores ``PRAGMA foreign_keys=ON`` unless the helper rolls
    back first. Without that rollback the read-back fails and the connection
    is invalidated, which this test detects: the in-memory database behind
    the fixture's single shared connection is gone, and ``physical`` is
    closed. The exception must reach the caller, and the write must not
    survive."""
    user = _account("member1", Role.STAFF)
    with pytest.raises(ZeroDivisionError):
        with _foreign_keys_off() as connection:
            physical = connection.connection.dbapi_connection
            connection.execute(sa.text(_ORPHAN_MEMBERSHIP), {"u": user.id})     # accepted: checks are off
            assert connection.execute(sa.text(_ORPHANS)).scalar() == 1
            if _db.engine.dialect.name == "sqlite":
                assert physical.in_transaction                                  # the PRAGMA-ignoring state
            1 / 0
    assert _db.session.scalar(sa.text(_ORPHANS)) == 0                          # the write was rolled back
    _db.session.rollback()
    assert _checks_on(physical)                                                # restored on that connection
    if isinstance(_db.engine.pool, sa.pool.StaticPool):                        # one shared connection (SQLite)
        with _db.engine.connect() as again:
            assert again.connection.dbapi_connection is physical               # kept, not invalidated
    with pytest.raises(sa.exc.IntegrityError, match=r"(?i)foreign key"):
        _set(_ORPHAN_MEMBERSHIP, u=user.id)
    _db.session.rollback()


def test_missing_installation_identity_fails_closed(db):
    _refused(_account("boss", Role.ADMIN), "missing_identity")


@pytest.mark.parametrize("extra", ["branch", "organization"])
def test_an_invalid_installation_identity_fails_closed(local, extra):
    admin = _enrolled(local, "boss", Role.ADMIN)
    if extra == "organization":
        _set("INSERT INTO organizations (public_id, name, created_at) VALUES "
             "('0b1c2d3e-0000-4000-8000-0000000000c1', 'Second', CURRENT_TIMESTAMP)")
    else:
        _set("INSERT INTO branches (organization_id, public_id, name, created_at) VALUES "
             "(:o, '0b1c2d3e-0000-4000-8000-0000000000c2', 'Second', CURRENT_TIMESTAMP)", o=local.organization_id)
    _refused(admin, "invalid_identity")


# Pure-rule tests for states the database constraints prevent.

def _identity(**overrides):
    row = dict(installation_id=1, branch_id=2, branch_organization_id=3, organization_id=3, installations=1,
               organizations=1, branches=1)
    row.update(overrides)
    return [SimpleNamespace(**row)]


def _row(membership_id=10, membership_organization_id=3, membership_status="active", organization_role=None,
         assignment_id=20, assignment_organization_id=3, branch_id=2, assignment_status="active",
         branch_role="staff"):
    return SimpleNamespace(**locals())


_STAFF = AuthContext(user_id=7, global_role=Role.STAFF, is_active=True, staff_approved=True)


@pytest.mark.parametrize("rows, code", [
    ([], "missing_identity"),
    (_identity(installation_id=2), "invalid_identity"),
    (_identity(installations=2), "invalid_identity"),
    (_identity(branches=0), "invalid_identity"),
    (_identity(branch_id=None), "invalid_identity"),
    (_identity(organization_id=None), "invalid_identity"),
    (_identity(branch_organization_id=4), "invalid_identity"),
    (_identity() * 2, "invalid_identity"),
])
def test_identity_rules(rows, code):
    with pytest.raises(WorkforceContextError) as exc:
        local_organization_and_branch_ids(rows)
    assert exc.value.code == code


def test_identity_rules_accept_a_valid_identity():
    assert local_organization_and_branch_ids(_identity()) == (3, 2)


@pytest.mark.parametrize("rows, code", [
    ([_row(), _row(membership_id=11, assignment_id=21)], "ambiguous_membership"),
    ([_row(), _row(assignment_id=21)], "ambiguous_assignment"),
    ([_row(), _row(assignment_id=21, branch_id=5)], "wrong_branch"),
    ([_row(membership_status="suspended")], "revoked_membership"),
    ([_row(assignment_status="suspended")], "revoked_assignment"),
    ([_row(organization_role="boss")], "role_mismatch"),
    ([_row(branch_role="Staff")], "role_mismatch"),
    ([_row(assignment_id=None, assignment_organization_id=None, branch_id=None, assignment_status=None,
           branch_role=None)], "missing_assignment"),
])
def test_membership_rules_for_states_the_constraints_prevent(rows, code):
    with pytest.raises(WorkforceContextError) as exc:
        workforce_context(_STAFF, 3, 2, rows)
    assert exc.value.code == code


def test_membership_rules_accept_a_valid_staff_member():
    context = workforce_context(_STAFF, 3, 2, [_row()])
    assert (context.organization_id, context.branch_id, context.organization_role, context.branch_role) \
        == (3, 2, None, BranchRole.STAFF)


# --- database failure and transaction safety ----------------------------------------------------------------


def test_a_database_read_failure_fails_closed_without_leaking_or_touching_the_callers_work(local):
    admin = _enrolled(local, "boss", Role.ADMIN)
    flushed = User(username="flushed", email="flushed@example.com", password_hash=hash_password(PASSWORD),
                   role=Role.CUSTOMER, staff_approved=False)
    _db.session.add(flushed)
    _db.session.flush()                                   # caller work, flushed but uncommitted
    pending = User(username="pending", email="pending@example.com", password_hash=hash_password(PASSWORD),
                   role=Role.CUSTOMER, staff_approved=False)
    _db.session.add(pending)                              # caller work, not yet flushed

    def fail(conn, cursor, statement, parameters, context, executemany):
        if "FROM installation i" in statement:
            raise sa.exc.OperationalError(statement, {}, Exception("mysql://qj:secret-password@db-host/qj"))
    sa.event.listen(_db.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(WorkforceContextError) as exc:
            resolve_auth_context(admin)
    finally:
        sa.event.remove(_db.engine, "before_cursor_execute", fail)

    assert exc.value.code == "database_error"
    assert "OperationalError" in str(exc.value)
    assert "secret" not in str(exc.value) and "db-host" not in str(exc.value)
    assert pending in _db.session.new                    # not flushed by the resolver ...
    _db.session.commit()                                  # ... nor rolled back: the caller's transaction is intact
    names = set(_db.session.scalars(sa.select(User.username)))
    assert {"flushed", "pending"} <= names


def test_resolution_is_read_only_and_never_flushes_the_callers_pending_work(local):
    """Includes the case that first exposed the risk: ``admin``'s attributes
    are expired (by the commit in _enrolled), so reading them reloads the row
    -- which must not flush the caller's pending object."""
    admin = _enrolled(local, "boss", Role.ADMIN)
    before = _snapshot()
    pending = User(username="pending", email="pending@example.com", password_hash=hash_password(PASSWORD),
                   role=Role.CUSTOMER, staff_approved=False)
    _db.session.add(pending)
    context = resolve_auth_context(admin)
    assert context.organization_role == OrganizationRole.ADMIN
    assert pending in _db.session.new and not _db.session.dirty
    _db.session.expunge(pending)
    assert _snapshot() == before


def test_resolution_reads_two_bounded_queries(local):
    admin = _enrolled(local, "boss", Role.ADMIN)
    _db.session.refresh(admin)                    # the caller's row is loaded; count only the resolver
    with _statements() as seen:
        resolve_auth_context(admin)
    assert len(seen) == 2
    assert "FROM installation i" in seen[0] and "FROM organization_memberships m" in seen[1]


# --- trust boundaries --------------------------------------------------------------------------------------


def _request_context(app, user, **request):
    ctx = app.test_request_context(**request)
    ctx.push()
    from flask import session
    g.pop("current_user", None)                           # one app context per test (known issue #29)
    if user is not None:
        session["user_id"], session["sv"] = user.id, user.session_version
    return ctx, session


@pytest.mark.parametrize("request_kwargs", [
    {"path": "/?organization_id=999&branch_id=999&organization_role=owner&branch_role=manager"},
    {"path": "/", "method": "POST", "json": {"organization_id": 999, "branch_id": 999, "organization_role": "owner",
                                             "branch_role": "manager"}},
    {"path": "/", "headers": {"X-Organization-Id": "999", "X-Branch-Id": "999", "X-Organization-Role": "owner",
                              "X-Branch-Role": "manager"}},
], ids=["query", "body", "headers"])
def test_client_supplied_organization_branch_and_roles_have_no_effect(app, local, request_kwargs):
    cook = _enrolled(local, "cook", Role.STAFF)
    expected = resolve_auth_context(cook)
    ctx, _ = _request_context(app, cook, **request_kwargs)
    try:
        assert current_resolved_auth_context() == expected
    finally:
        ctx.pop()


def test_session_values_cannot_override_the_database_and_nothing_is_stored(app, local):
    cook = _enrolled(local, "cook", Role.STAFF)
    expected = resolve_auth_context(cook)
    ctx, session = _request_context(app, cook)
    try:
        session.update(organization_id=999, branch_id=999, organization_role="owner", branch_role="manager",
                       global_role="admin")
        assert current_resolved_auth_context() == expected
        assert set(session) == {"user_id", "sv", "organization_id", "branch_id", "organization_role",
                                "branch_role", "global_role"}   # only what the test itself put there
    finally:
        ctx.pop()


def test_the_context_is_rebuilt_from_the_current_database_state_on_every_request(app, local):
    cook = _enrolled(local, "cook", Role.STAFF)
    ctx, _ = _request_context(app, cook)
    try:
        assert current_resolved_auth_context().branch_role == BranchRole.STAFF
    finally:
        ctx.pop()
    org.revoke_membership(_membership(cook))              # between two requests
    ctx, _ = _request_context(app, cook)
    try:
        with pytest.raises(WorkforceContextError) as exc:
            current_resolved_auth_context()
        assert exc.value.code == "revoked_membership"
    finally:
        ctx.pop()


def test_the_existing_session_gate_still_decides_who_is_signed_in(app, local):
    """Anonymous, inactive and pending-staff sessions get None, exactly as
    get_current_user() decides; nothing is added to the session."""
    inactive = _enrolled(local, "gone", Role.STAFF, active=False)
    pending = _enrolled(local, "waiting", Role.STAFF, approved=False)
    for user in (None, inactive, pending):
        ctx, session = _request_context(app, user)
        try:
            assert current_resolved_auth_context() is None
            assert set(session) <= {"user_id", "sv"}
        finally:
            ctx.pop()


# --- compatibility -----------------------------------------------------------------------------------------


def test_the_existing_context_functions_are_unchanged(app, local):
    """auth_context_for_user() / current_auth_context() stay membership-blind
    (O3.2 pins them); only the new resolver reads membership."""
    admin = _enrolled(local, "boss", Role.ADMIN)
    assert auth_context_for_user(admin).organization_id is None
    ctx, _ = _request_context(app, admin)
    try:
        assert current_auth_context() == auth_context_for_user(admin)
    finally:
        ctx.pop()


def test_grants_and_role_grant_maps_are_unchanged():
    assert all(grants == frozenset() for grants in ORGANIZATION_ROLE_GRANTS.values())
    assert all(grants == frozenset() for grants in BRANCH_ROLE_GRANTS.values())
    assert COMPATIBILITY_GRANTS[Role.CUSTOMER] == frozenset()
    assert ac.WORKFORCE_ROLES == (Role.ADMIN, Role.STAFF)
