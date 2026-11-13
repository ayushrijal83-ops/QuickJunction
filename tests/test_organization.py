"""O2: organization membership and branch assignment -- models, constraints,
the service (``app/services/organization.py``), the account-creation hooks,
``flask organization members``, and the boundary that membership grants no
access.

Uses the standard ``app`` fixture (``create_all()``), so it runs on SQLite
and, with ``TEST_DATABASE_URL``, on MySQL. The last test is MySQL-only; CI's
MySQL job runs it in the O2 guard and fails unless it passes. The migration
is covered by tests/test_organization_migration.py.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from flask import g
from sqlalchemy.exc import IntegrityError

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.installation import Branch, Organization
from app.models.organization import BranchAssignment, LifecycleStatus, OrganizationMembership
from app.models.user import Role, User
from app.services import audit as audit_service
from app.services import organization as org
from app.services.accounts import AccountError, invite_staff
from app.services.auth import RegistrationError, RegistrationInput, create_admin
from app.services.installation import InstallationIdentityError
from tests.conftest import make_user, rejected_by_check_constraint

ACTIVE, REVOKED = LifecycleStatus.ACTIVE, LifecycleStatus.REVOKED
PASSWORD = "correct-horse-1"
O2_EVENTS = {AuditEvent.MEMBERSHIP_CREATED, AuditEvent.MEMBERSHIP_REVOKED, AuditEvent.MEMBERSHIP_REINSTATED,
             AuditEvent.BRANCH_ASSIGNMENT_CREATED, AuditEvent.BRANCH_ASSIGNMENT_REVOKED,
             AuditEvent.BRANCH_ASSIGNMENT_REINSTATED}


@pytest.fixture
def boss(installation_identity):
    return make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)


@pytest.fixture
def cook(installation_identity):
    return make_user(username="cook", email="cook@example.com", password=PASSWORD, role=Role.STAFF)


@pytest.fixture
def enrolled(installation_identity, boss, cook):
    """cook as an active member, actively assigned to the local branch."""
    _, branch = installation_identity
    membership = org.add_member(cook, actor=boss)
    assignment = org.assign_branch(membership, branch, actor=boss)
    return membership, assignment


@contextmanager
def refused(text):
    """OrganizationError whose messages mention ``text`` (ValidationError's
    own str() is generic; the messages live in ``.errors``)."""
    with pytest.raises(org.OrganizationError) as exc:
        yield
    assert text in json.dumps(exc.value.errors), exc.value.errors


def _o2_audit(event=None):
    rows = _db.session.query(AuditLog).filter(AuditLog.event_type.in_(O2_EVENTS)).order_by(AuditLog.id).all()
    return [r for r in rows if event is None or r.event_type == event]


def _counts():
    _db.session.rollback()
    return (_db.session.query(OrganizationMembership).count(), _db.session.query(BranchAssignment).count(),
            len(_o2_audit()))


def _second_org_branch():
    other = Organization(name="Elsewhere")
    other_branch = Branch(organization=other, name="Elsewhere")
    _db.session.add_all([other, other_branch])
    _db.session.commit()
    return other, other_branch


# --- constraints (database level) -------------------------------------------------------


def test_one_membership_per_user_and_organization(enrolled, installation_identity):
    organization, _ = installation_identity
    membership, _ = enrolled
    with pytest.raises(IntegrityError):
        _db.session.execute(sa.text("INSERT INTO organization_memberships (organization_id, user_id, status, "
                                    "created_at) VALUES (:o, :u, 'active', CURRENT_TIMESTAMP)"),
                            {"o": organization.id, "u": membership.user_id})
    _db.session.rollback()


def test_one_assignment_per_membership_and_branch(enrolled):
    membership, assignment = enrolled
    with pytest.raises(IntegrityError):
        _db.session.execute(sa.text("INSERT INTO branch_assignments (organization_id, membership_id, branch_id, "
                                    "status, created_at) VALUES (:o, :m, :b, 'active', CURRENT_TIMESTAMP)"),
                            {"o": membership.organization_id, "m": membership.id, "b": assignment.branch_id})
    _db.session.rollback()


def test_composite_foreign_keys_reject_a_cross_organization_assignment(enrolled):
    """Whichever organization id the row claims, one of the two composite
    keys fails: the membership is in one organization, the branch in another."""
    membership, _ = enrolled
    other, other_branch = _second_org_branch()
    insert = sa.text("INSERT INTO branch_assignments (organization_id, membership_id, branch_id, status, created_at)"
                     " VALUES (:o, :m, :b, 'active', CURRENT_TIMESTAMP)")
    for claimed_org in (membership.organization_id, other.id):
        with pytest.raises(IntegrityError):
            _db.session.execute(insert, {"o": claimed_org, "m": membership.id, "b": other_branch.id})
        _db.session.rollback()
    assert _db.session.query(BranchAssignment).filter_by(branch_id=other_branch.id).count() == 0


def test_status_must_be_active_or_revoked(enrolled):
    membership, assignment = enrolled
    for table, row_id in (("organization_memberships", membership.id), ("branch_assignments", assignment.id)):
        with rejected_by_check_constraint():
            _db.session.execute(sa.text(f"UPDATE {table} SET status = 'suspended' WHERE id = :i"), {"i": row_id})
        _db.session.rollback()


def test_status_and_revoked_at_must_agree(enrolled):
    membership, assignment = enrolled
    for table, row_id in (("organization_memberships", membership.id), ("branch_assignments", assignment.id)):
        with rejected_by_check_constraint():  # revoked without a revocation time
            _db.session.execute(sa.text(f"UPDATE {table} SET status = 'revoked' WHERE id = :i"), {"i": row_id})
        _db.session.rollback()
        with rejected_by_check_constraint():  # active with one
            _db.session.execute(sa.text(f"UPDATE {table} SET revoked_at = CURRENT_TIMESTAMP WHERE id = :i"),
                                {"i": row_id})
        _db.session.rollback()


@pytest.mark.parametrize("statement", [
    "DELETE FROM users WHERE id = :user_id",
    "DELETE FROM organization_memberships WHERE id = :membership_id",
    "DELETE FROM branches WHERE id = :branch_id",
    "DELETE FROM organizations WHERE id = :organization_id",
])
def test_deleting_a_referenced_row_is_restricted(enrolled, statement):
    membership, assignment = enrolled
    params = {"user_id": membership.user_id, "membership_id": membership.id, "branch_id": assignment.branch_id,
              "organization_id": membership.organization_id}
    with pytest.raises(IntegrityError):
        _db.session.execute(sa.text(statement), params)
    _db.session.rollback()
    assert _counts()[:2] == (1, 1)


# --- add_member / assign_branch -----------------------------------------------------------


def test_add_member_and_assign_branch_write_rows_and_audit(installation_identity, boss, cook):
    organization, branch = installation_identity
    membership = org.add_member(cook, actor=boss)
    assignment = org.assign_branch(membership, branch, actor=boss)

    assert (membership.organization_id, membership.user_id, membership.status) == (organization.id, cook.id, ACTIVE)
    assert membership.created_by_id == boss.id and membership.revoked_at is None
    assert (assignment.organization_id, assignment.membership_id, assignment.branch_id) == (
        organization.id, membership.id, branch.id)
    assert assignment.status == ACTIVE and assignment.created_by_id == boss.id

    created, assigned = _o2_audit()
    assert created.event_type == AuditEvent.MEMBERSHIP_CREATED and created.user_id == boss.id
    assert json.loads(created.metadata_json) == {"membership_id": membership.id, "target_user_id": cook.id,
                                                 "organization_id": organization.id}
    assert assigned.event_type == AuditEvent.BRANCH_ASSIGNMENT_CREATED
    assert json.loads(assigned.metadata_json)["assignment_id"] == assignment.id


def test_pending_and_inactive_staff_can_be_members(installation_identity, boss):
    pending = make_user(username="pend", email="pend@example.com", role=Role.STAFF, staff_approved=False)
    inactive = make_user(username="gone", email="gone@example.com", role=Role.STAFF, is_active=False)
    for user in (pending, inactive):
        assert org.add_member(user, actor=boss).status == ACTIVE


def test_customers_cannot_be_members(installation_identity, boss):
    diner = make_user(username="diner", email="diner@example.com", role=Role.CUSTOMER)
    with refused("customer"):
        org.add_member(diner, actor=boss)
    assert _counts() == (0, 0, 0)


def test_duplicate_membership_and_assignment_are_refused(enrolled, installation_identity, boss, cook):
    membership, _ = enrolled
    _, branch = installation_identity
    with refused("already has"):
        org.add_member(cook, actor=boss)
    with refused("already has"):
        org.assign_branch(membership, branch, actor=boss)
    assert _counts() == (1, 1, 2)


def test_assigning_a_revoked_membership_is_refused(installation_identity, boss, cook):
    _, branch = installation_identity
    membership = org.add_member(cook, actor=boss)
    org.revoke_membership(membership, actor=boss)
    with refused("revoked membership"):
        org.assign_branch(membership, branch, actor=boss)
    assert _db.session.query(BranchAssignment).count() == 0


def test_assigning_another_organizations_branch_is_refused(installation_identity, boss, cook):
    membership = org.add_member(cook, actor=boss)
    _, other_branch = _second_org_branch()
    with refused("different organization"):
        org.assign_branch(membership, other_branch, actor=boss)
    assert _db.session.query(BranchAssignment).count() == 0


def test_only_an_active_admin_may_act(installation_identity, cook):
    other = make_user(username="cook2", email="cook2@example.com", role=Role.STAFF)
    retired = make_user(username="retired", email="retired@example.com", role=Role.ADMIN, is_active=False)
    for actor in (other, retired):
        with refused("active administrator"):
            org.add_member(cook, actor=actor)
    assert _counts() == (0, 0, 0)


def test_add_member_without_an_identity_fails_and_creates_nothing(db):
    cook = make_user(username="cook", email="cook@example.com", role=Role.STAFF)
    with pytest.raises(InstallationIdentityError):
        org.add_member(cook)
    assert _counts() == (0, 0, 0)


# --- revoke / reinstate --------------------------------------------------------------------


def test_revoking_a_membership_revokes_its_assignments_atomically(enrolled, boss):
    membership, assignment = enrolled
    assert org.revoke_membership(membership, actor=boss) is True
    _db.session.expire_all()
    assert membership.status == REVOKED and membership.revoked_at and membership.revoked_by_id == boss.id
    assert assignment.status == REVOKED and assignment.revoked_at == membership.revoked_at
    revoked_assignment, revoked_membership = _o2_audit()[2:]
    assert revoked_assignment.event_type == AuditEvent.BRANCH_ASSIGNMENT_REVOKED
    assert json.loads(revoked_assignment.metadata_json)["reason"] == "membership_revoked"
    assert revoked_membership.event_type == AuditEvent.MEMBERSHIP_REVOKED
    assert json.loads(revoked_membership.metadata_json)["revoked_assignment_ids"] == [assignment.id]


def test_reinstating_a_membership_does_not_reinstate_assignments(enrolled, boss):
    membership, assignment = enrolled
    org.revoke_membership(membership, actor=boss)
    assert org.reinstate_membership(membership, actor=boss) is True
    _db.session.expire_all()
    assert membership.status == ACTIVE and membership.revoked_at is None and membership.revoked_by_id is None
    assert assignment.status == REVOKED
    assert _o2_audit()[-1].event_type == AuditEvent.MEMBERSHIP_REINSTATED


def test_assignment_revoke_and_reinstate(enrolled, boss):
    membership, assignment = enrolled
    assert org.revoke_assignment(assignment, actor=boss) is True
    _db.session.expire_all()
    assert assignment.status == REVOKED and membership.status == ACTIVE
    assert org.reinstate_assignment(assignment, actor=boss) is True
    _db.session.expire_all()
    assert assignment.status == ACTIVE and assignment.revoked_at is None
    assert [r.event_type for r in _o2_audit()[2:]] == [AuditEvent.BRANCH_ASSIGNMENT_REVOKED,
                                                       AuditEvent.BRANCH_ASSIGNMENT_REINSTATED]


def test_reinstating_an_assignment_under_a_revoked_membership_is_refused(enrolled, boss):
    membership, assignment = enrolled
    org.revoke_membership(membership, actor=boss)
    with refused("Reinstate the membership"):
        org.reinstate_assignment(assignment, actor=boss)
    _db.session.expire_all()
    assert assignment.status == REVOKED


def test_repeated_revoke_and_reinstate_are_no_ops(enrolled, boss):
    membership, assignment = enrolled
    assert org.reinstate_membership(membership, actor=boss) is False  # already active
    assert org.reinstate_assignment(assignment, actor=boss) is False
    org.revoke_membership(membership, actor=boss)
    before = _counts()
    _db.session.expire_all()
    revoked_at = membership.revoked_at
    assert org.revoke_membership(membership, actor=boss) is False
    assert org.revoke_assignment(assignment, actor=boss) is False
    assert _counts() == before
    _db.session.expire_all()
    assert membership.revoked_at == revoked_at


def test_a_failing_audit_write_rolls_back_the_whole_revocation(enrolled, boss, monkeypatch):
    membership, assignment = enrolled
    real = audit_service.record_event

    def broken(event_type, **kwargs):
        if event_type == AuditEvent.MEMBERSHIP_REVOKED:
            raise RuntimeError("audit store unavailable")
        return real(event_type, **kwargs)

    monkeypatch.setattr(audit_service, "record_event", broken)
    with pytest.raises(RuntimeError):
        org.revoke_membership(membership, actor=boss)
    _db.session.expire_all()
    assert membership.status == ACTIVE and assignment.status == ACTIVE  # the assignment revoke rolled back too
    assert len(_o2_audit()) == 2


# --- account-creation hooks ---------------------------------------------------------------


def test_invited_staff_is_enrolled_in_the_same_transaction(installation_identity, boss):
    organization, branch = installation_identity
    user, _ = invite_staff(boss, "newcook", "newcook@example.com")
    membership = _db.session.query(OrganizationMembership).filter_by(user_id=user.id).one()
    assert (membership.organization_id, membership.status, membership.created_by_id) == (organization.id, ACTIVE,
                                                                                          boss.id)
    [assignment] = membership.assignments
    assert (assignment.branch_id, assignment.status) == (branch.id, ACTIVE)
    assert [r.event_type for r in _o2_audit()] == [AuditEvent.MEMBERSHIP_CREATED,
                                                   AuditEvent.BRANCH_ASSIGNMENT_CREATED]
    # The account itself is exactly what M4 creates.
    assert (user.role, user.is_active, user.staff_approved) == (Role.STAFF, False, True)


def test_failed_invitation_leaves_no_account_and_no_membership(installation_identity, boss):
    def broken_audit(user):
        raise RuntimeError("audit store unavailable")

    users_before = _db.session.query(User).count()
    with pytest.raises(RuntimeError):
        invite_staff(boss, "newcook", "newcook@example.com", audit=broken_audit)
    assert _db.session.query(User).count() == users_before
    assert _counts() == (0, 0, 0)


def test_invitation_without_an_identity_is_refused_cleanly(db):
    boss = make_user(username="boss", email="boss@example.com", role=Role.ADMIN)
    with pytest.raises(AccountError) as exc:
        invite_staff(boss, "newcook", "newcook@example.com")
    assert "No installation identity" in exc.value.errors["_general"][0]
    assert _db.session.query(User).filter_by(username="newcook").count() == 0
    assert _counts() == (0, 0, 0)


def test_create_admin_is_enrolled_atomically(installation_identity):
    admin = create_admin(RegistrationInput("owner", "owner@example.com", PASSWORD, PASSWORD))
    membership = _db.session.query(OrganizationMembership).filter_by(user_id=admin.id).one()
    assert membership.created_by_id is None and [a.status for a in membership.assignments] == [ACTIVE]
    assert all(r.user_id is None for r in _o2_audit())


def test_create_admin_failure_leaves_no_membership(installation_identity, monkeypatch):
    real = audit_service.record_event

    def broken(event_type, **kwargs):
        if event_type == AuditEvent.ADMIN_CREATED:
            raise RuntimeError("audit store unavailable")
        return real(event_type, **kwargs)

    monkeypatch.setattr("app.services.audit.record_event", broken)
    with pytest.raises(RuntimeError):
        create_admin(RegistrationInput("owner", "owner@example.com", PASSWORD, PASSWORD))
    assert _db.session.query(User).filter_by(username="owner").count() == 0
    assert _counts() == (0, 0, 0)


def test_create_admin_without_an_identity_is_refused(db):
    with pytest.raises(RegistrationError) as exc:
        create_admin(RegistrationInput("owner", "owner@example.com", PASSWORD, PASSWORD))
    assert "No installation identity" in exc.value.errors["_general"][0]
    assert _db.session.query(User).count() == 0


def test_customer_registration_creates_no_membership(client, installation_identity):
    from tests.conftest import csrf_token

    client.post("/register", data={"csrf_token": csrf_token(client, "/register"), "username": "diner",
                                   "email": "diner@example.com", "password": PASSWORD, "password_confirm": PASSWORD})
    assert _db.session.query(User).filter_by(username="diner").one().role == Role.CUSTOMER
    assert _counts() == (0, 0, 0)


# --- membership is not access ---------------------------------------------------------------


def _login(client, username, portal):
    g.pop("current_user", None)
    return client.post(f"/login/{portal}", data={"username": username, "password": PASSWORD})


@pytest.mark.parametrize("portal, probe", [("staff", "/_test/staff"), ("admin", "/_test/admin")])
def test_revoked_membership_does_not_change_sign_in_or_permissions(app, client, installation_identity, boss,
                                                                   portal, probe):
    """Pinned O2 boundary: until O3, membership is a record, not a gate."""
    user = boss if portal == "admin" else make_user(username="cook", email="cook@example.com",
                                                    password=PASSWORD, role=Role.STAFF)
    _, branch = installation_identity
    membership = org.add_member(user, actor=boss if user is not boss else None)
    org.assign_branch(membership, branch)
    org.revoke_membership(membership)
    account = _db.session.execute(sa.text(
        "SELECT role, is_active, staff_approved, session_version, password_hash FROM users WHERE id = :i"),
        {"i": user.id}).one()

    assert _login(client, user.username, portal).status_code == 302
    g.pop("current_user", None)
    assert client.get(probe).status_code == 200
    _db.session.rollback()
    after = _db.session.execute(sa.text(
        "SELECT role, is_active, staff_approved, session_version, password_hash FROM users WHERE id = :i"),
        {"i": user.id}).one()
    assert after == account


# --- list_members / check_consistency / CLI ---------------------------------------------------


def test_list_members_reports_accounts_and_assignments(enrolled, cook):
    [view] = org.list_members()
    assert (view.username, view.role, view.account_active, view.status) == ("cook", Role.STAFF, True, ACTIVE)
    assert [(a.branch_name, a.status) for a in view.assignments] == [("Quick Junction", ACTIVE)]


def test_consistency_check_reports_each_kind_of_gap(installation_identity, boss, cook):
    _, branch = installation_identity
    assert {i.kind for i in org.check_consistency()} == {"missing_membership"}  # boss and cook inserted directly
    lonely = org.add_member(boss)  # no assignment
    member = org.add_member(cook)
    org.assign_branch(member, branch)
    diner = make_user(username="diner", email="diner@example.com", role=Role.CUSTOMER)
    _db.session.execute(sa.text("INSERT INTO organization_memberships (organization_id, user_id, status, created_at)"
                                " VALUES (:o, :u, 'active', CURRENT_TIMESTAMP)"),
                        {"o": lonely.organization_id, "u": diner.id})
    # A revoked membership whose assignment was left active (only possible by bypassing the service).
    _db.session.execute(sa.text("UPDATE organization_memberships SET status = 'revoked', "
                                "revoked_at = CURRENT_TIMESTAMP WHERE id = :i"), {"i": member.id})
    _db.session.commit()
    kinds = sorted(i.kind for i in org.check_consistency())
    assert kinds == ["active_assignment_under_revoked_membership", "customer_member", "missing_assignment",
                     "missing_assignment"]  # boss (no assignment) and the customer row


def test_cli_lists_members_and_writes_nothing(app, enrolled, boss, installation_identity):
    org.assign_branch(org.add_member(boss), installation_identity[1])
    before = _counts()
    result = app.test_cli_runner().invoke(args=["organization", "members"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Organization: Quick Junction" in out and "Members:      2" in out
    assert "cook  (account: staff, active)" in out
    assert "Membership:  active" in out and "Branch:      Quick Junction -- active" in out
    assert "Consistency:  OK" in out
    assert "does not grant access" in out
    assert _counts() == before


def test_cli_reports_inconsistencies_with_exit_status_1(app, installation_identity, boss):
    invite_staff(boss, "newcook", "newcook@example.com")  # enrolled; boss (inserted directly) is not
    result = app.test_cli_runner().invoke(args=["organization", "members"])
    assert result.exit_code == 1
    assert "newcook  (account: staff, invited)" in result.output
    assert "[missing_membership] boss (admin) has no organization membership." in result.output


def test_cli_without_an_identity_fails_clearly(app, db):
    result = app.test_cli_runner().invoke(args=["organization", "members"])
    assert result.exit_code == 1 and "No installation identity" in result.output
    assert _counts() == (0, 0, 0)


# --- MySQL only ----------------------------------------------------------------------------


def test_mysql_concurrent_add_member_creates_exactly_one(app, installation_identity, boss, cook):
    """Two simultaneous add_member calls for one account, each in its own
    thread, session and connection: the unique key decides, exactly one
    membership exists, and the loser gets an OrganizationError."""
    if _db.engine.dialect.name != "mysql":
        pytest.skip("needs TEST_DATABASE_URL=mysql+pymysql://... (CI's MySQL job requires it to run)")
    cook_id, boss_id = cook.id, boss.id
    _db.session.commit()
    barrier, results = threading.Barrier(2), []

    def attempt():
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                results.append(org.add_member(_db.session.get(User, cook_id), actor=_db.session.get(User, boss_id)).id)
            except Exception as exc:  # asserted below
                results.append(exc)
            finally:
                _db.session.remove()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    wins = [r for r in results if isinstance(r, int)]
    losses = [r for r in results if isinstance(r, org.OrganizationError)]
    assert len(wins) == 1 and len(losses) == 1, results
    _db.session.rollback()
    assert _db.session.query(OrganizationMembership).filter_by(user_id=cook_id).count() == 1
    assert len(_o2_audit(AuditEvent.MEMBERSHIP_CREATED)) == 1


# --- scripts/seed_demo.py ---------------------------------------------------------------------


def _run_seed(app, monkeypatch):
    import scripts.seed_demo as seed_demo

    monkeypatch.setattr(seed_demo, "create_app", lambda: app)  # never the .env database
    status = seed_demo.main()
    _db.session.rollback()
    return status


def test_seed_demo_enrolls_staff_and_admin_but_not_the_customer(app, installation_identity, monkeypatch):
    assert _run_seed(app, monkeypatch) == 0
    members = {m.user.username for m in _db.session.query(OrganizationMembership)}
    assert members == {"staff", "admin"}
    assert _db.session.query(BranchAssignment).count() == 2
    assert org.check_consistency() == []
    assert _run_seed(app, monkeypatch) == 0  # idempotent: nothing duplicated
    assert _counts()[:2] == (2, 2)


def test_seed_demo_without_an_identity_creates_no_accounts(app, db, monkeypatch, capsys):
    assert _run_seed(app, monkeypatch) == 1
    assert "Demo accounts NOT created" in capsys.readouterr().err
    assert _db.session.query(User).count() == 0
    assert _counts() == (0, 0, 0)
