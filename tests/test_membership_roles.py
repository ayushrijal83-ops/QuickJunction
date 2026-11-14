"""O3.3: stored organization and branch membership roles -- the services,
the database CHECKs, the consistency check and CLI, and the guarantee that a
stored role changes no one's access.

The fixed rule: an ADMIN's membership is ``admin`` and its assignments carry
no branch role; a STAFF member's membership has no organization role and its
assignments are ``staff``. Nothing writes ``owner`` or ``manager``.
Migration 9a3e6c1d7b54 is covered by tests/test_membership_roles_migration.py.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.extensions import db as _db
from app.models import organization as org_models
from app.models.organization import BranchAssignment, BranchRole, OrganizationMembership, OrganizationRole
from app.models.user import Role, User
from app.services import organization as org
from app.services.accounts import invite_staff
from app.services.auth import RegistrationInput, create_admin
from app.utils import permissions
from app.utils.permissions import (
    BRANCH_ROLE_GRANTS, COMPATIBILITY_GRANTS, ORGANIZATION_ROLE_GRANTS, AuthContext, auth_context_for_user,
    permissions_for)
from tests.conftest import make_user, rejected_by_check_constraint

PASSWORD = "correct-horse-1"


@pytest.fixture
def boss(installation_identity):
    return make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)


def _enroll(user, actor=None):
    _, branch = org.local_organization_and_branch()
    membership = org.add_member(user, actor=actor)
    assignment = org.assign_branch(membership, branch, actor=actor)
    return membership, assignment


def _roles(user):
    _db.session.rollback()
    membership = _db.session.execute(sa.select(OrganizationMembership).filter_by(user_id=user.id)).scalar_one()
    branch_roles = [a.branch_role for a in _db.session.execute(sa.select(BranchAssignment).filter_by(
        membership_id=membership.id)).scalars()]
    return membership.organization_role, branch_roles


# --- the enums: one class, two import paths --------------------------------------------------------


def test_role_enums_live_with_their_columns_and_keep_their_permissions_import():
    assert permissions.OrganizationRole is org_models.OrganizationRole is OrganizationRole
    assert permissions.BranchRole is org_models.BranchRole is BranchRole
    assert [r.value for r in OrganizationRole] == ["owner", "admin"]
    assert [r.value for r in BranchRole] == ["manager", "staff"]


def test_role_rule_maps_each_global_role():
    assert org.organization_role_for(Role.ADMIN) == OrganizationRole.ADMIN
    assert org.organization_role_for(Role.STAFF) is None
    assert org.branch_role_for(Role.STAFF) == BranchRole.STAFF
    assert org.branch_role_for(Role.ADMIN) is None  # no branch manager is inferred


# --- every creation path ---------------------------------------------------------------------------


def test_create_admin_stores_the_admin_role_for_a_pending_admin(installation_identity):
    admin = create_admin(RegistrationInput(username="newadmin", email="newadmin@example.com",
                                           password=PASSWORD, password_confirm=PASSWORD))
    assert admin.role == Role.ADMIN and admin.staff_approved is False  # create-admin leaves it false
    assert _roles(admin) == (OrganizationRole.ADMIN, [None])


def test_invite_staff_stores_the_staff_branch_role(boss):
    _enroll(boss)
    staff, _ = invite_staff(boss, "newcook", "newcook@example.com")
    assert staff.is_active is False  # invited: inactive until the code is redeemed
    assert _roles(staff) == (None, [BranchRole.STAFF])


def test_seed_demo_stores_roles(app, installation_identity, monkeypatch):
    import scripts.seed_demo as seed_demo

    monkeypatch.setattr(seed_demo, "create_app", lambda: app)  # never the .env database
    assert seed_demo.main() == 0
    _db.session.rollback()
    by_name = {u.username: u for u in _db.session.execute(sa.select(User)).scalars()}
    assert _roles(by_name["admin"]) == (OrganizationRole.ADMIN, [None])
    assert _roles(by_name["staff"]) == (None, [BranchRole.STAFF])


@pytest.mark.parametrize("role, approved, active, expected", [
    (Role.ADMIN, True, True, (OrganizationRole.ADMIN, [None])),
    (Role.ADMIN, False, True, (OrganizationRole.ADMIN, [None])),     # pending admin
    (Role.ADMIN, False, False, (OrganizationRole.ADMIN, [None])),    # inactive admin
    (Role.STAFF, True, True, (None, [BranchRole.STAFF])),
    (Role.STAFF, False, True, (None, [BranchRole.STAFF])),           # pending staff
    (Role.STAFF, True, False, (None, [BranchRole.STAFF])),           # inactive staff
])
def test_add_member_and_assign_branch_derive_the_role(boss, role, approved, active, expected):
    user = make_user(username="member1", email="member1@example.com", role=role, staff_approved=approved,
                     is_active=active)
    _enroll(user, actor=boss)
    assert _roles(user) == expected


def test_reinstating_keeps_the_stored_roles(boss):
    cook = make_user(username="cook", email="cook@example.com", role=Role.STAFF)
    for user in (boss, cook):
        membership, assignment = _enroll(user)
        before = _roles(user)
        org.revoke_membership(membership)
        assert _roles(user) == before  # revocation keeps the role on both rows
        org.reinstate_membership(membership)
        org.reinstate_assignment(assignment)
        assert _roles(user) == before


def test_no_path_writes_owner_or_manager(boss):
    _enroll(boss)
    invite_staff(boss, "newcook", "newcook@example.com")
    create_admin(RegistrationInput(username="second", email="second@example.com", password=PASSWORD,
                                   password_confirm=PASSWORD))
    _db.session.rollback()
    assert _db.session.scalar(sa.select(sa.func.count()).select_from(OrganizationMembership).where(
        OrganizationMembership.organization_role == OrganizationRole.OWNER)) == 0
    assert _db.session.scalar(sa.select(sa.func.count()).select_from(BranchAssignment).where(
        BranchAssignment.branch_role == BranchRole.MANAGER)) == 0


# --- database CHECKs --------------------------------------------------------------------------------


@pytest.mark.parametrize("table, column, value", [
    ("organization_memberships", "organization_role", "manager"),
    ("organization_memberships", "organization_role", "staff"),
    ("organization_memberships", "organization_role", "Admin"),
    ("organization_memberships", "organization_role", ""),
    ("branch_assignments", "branch_role", "admin"),
    ("branch_assignments", "branch_role", "owner"),
    ("branch_assignments", "branch_role", "Staff"),
    ("branch_assignments", "branch_role", ""),
])
def test_role_checks_reject_values_outside_their_allow_list(boss, table, column, value):
    _enroll(boss)
    with rejected_by_check_constraint():
        _db.session.execute(sa.text(f"UPDATE {table} SET {column} = :v"), {"v": value})
    _db.session.rollback()


@pytest.mark.parametrize("table, column, values", [
    ("organization_memberships", "organization_role", ("owner", "admin", None)),
    ("branch_assignments", "branch_role", ("manager", "staff", None)),
])
def test_role_checks_accept_their_values_and_null(boss, table, column, values):
    """The database allows 'owner' and 'manager' (D4); only the application
    never writes them. Rolled back each time."""
    _enroll(boss)
    for value in values:
        _db.session.execute(sa.text(f"UPDATE {table} SET {column} = :v"), {"v": value})
        assert _db.session.execute(sa.text(f"SELECT {column} FROM {table}")).scalar() == value
        _db.session.rollback()


# --- consistency check and CLI ----------------------------------------------------------------------


def test_correctly_enrolled_members_are_consistent(boss):
    _enroll(boss)
    _enroll(make_user(username="cook", email="cook@example.com", role=Role.STAFF), actor=boss)
    assert org.check_consistency() == []


def test_consistency_reports_role_mismatches_and_an_owner(boss):
    cook = make_user(username="cook", email="cook@example.com", role=Role.STAFF)
    boss_membership, boss_assignment = _enroll(boss)
    cook_membership, cook_assignment = _enroll(cook)
    sets = [("organization_memberships", "organization_role", "owner", boss_membership.id),
            ("branch_assignments", "branch_role", "manager", boss_assignment.id),
            ("organization_memberships", "organization_role", "admin", cook_membership.id),
            ("branch_assignments", "branch_role", None, cook_assignment.id)]
    for table, column, value, row_id in sets:
        _db.session.execute(sa.text(f"UPDATE {table} SET {column} = :v WHERE id = :i"), {"v": value, "i": row_id})
    _db.session.commit()
    issues = org.check_consistency()
    assert sorted(i.kind for i in issues) == ["role_mismatch", "role_mismatch", "role_mismatch", "unexpected_owner"]
    text = "\n".join(i.message for i in issues)
    assert f"assignment {boss_assignment.id} has branch role manager, expected none" in text
    assert f"membership {cook_membership.id} has organization role admin, expected none" in text
    assert f"assignment {cook_assignment.id} has branch role none, expected staff" in text


def test_consistency_reports_a_revoked_membership_without_an_assignment_row(boss):
    membership = org.add_member(boss)  # add_member alone writes no assignment
    assert [i.kind for i in org.check_consistency()] == ["missing_assignment"]
    org.revoke_membership(membership)
    assert [i.kind for i in org.check_consistency()] == ["membership_without_assignment"]


def test_customer_member_is_reported_once_not_as_a_role_mismatch(boss):
    organization, _ = org.local_organization_and_branch()
    diner = make_user(username="diner", email="diner@example.com", role=Role.CUSTOMER)
    _enroll(boss)
    _db.session.execute(sa.text("INSERT INTO organization_memberships (organization_id, user_id, status, created_at)"
                                " VALUES (:o, :u, 'active', CURRENT_TIMESTAMP)"),
                        {"o": organization.id, "u": diner.id})
    _db.session.commit()
    assert sorted(i.kind for i in org.check_consistency()) == ["customer_member", "missing_assignment"]


def test_cli_prints_the_stored_roles(app, boss):
    _enroll(boss)
    _enroll(make_user(username="cook", email="cook@example.com", role=Role.STAFF), actor=boss)
    result = app.test_cli_runner().invoke(args=["organization", "members"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "boss  (account: admin, active)" in out
    assert "Membership:  active (organization role: admin)" in out
    assert "Branch:      Quick Junction -- active (branch role: none)" in out
    assert "Membership:  active (organization role: none)" in out
    assert "Branch:      Quick Junction -- active (branch role: staff)" in out
    assert "Stored organization and branch roles grant nothing yet." in out


def test_cli_exits_1_on_a_role_mismatch(app, boss):
    membership, _ = _enroll(boss)
    _db.session.execute(sa.text("UPDATE organization_memberships SET organization_role = NULL WHERE id = :i"),
                        {"i": membership.id})
    _db.session.commit()
    result = app.test_cli_runner().invoke(args=["organization", "members"])
    assert result.exit_code == 1
    assert "[role_mismatch] boss (admin)" in result.output


# --- stored roles grant nothing -----------------------------------------------------------------------


def test_membership_role_grant_maps_are_still_empty():
    assert all(grants == frozenset() for grants in ORGANIZATION_ROLE_GRANTS.values())
    assert all(grants == frozenset() for grants in BRANCH_ROLE_GRANTS.values())


ACCOUNT_STATES = [  # role, staff_approved, is_active, eligible under the approved policy
    (Role.ADMIN, True, True, True),
    (Role.ADMIN, False, True, True),     # pending ADMIN: eligible
    (Role.ADMIN, False, False, False),   # inactive ADMIN
    (Role.STAFF, True, True, True),
    (Role.STAFF, False, True, False),    # pending STAFF
    (Role.STAFF, True, False, False),    # inactive / invited STAFF
]


@pytest.mark.parametrize("role, approved, active, eligible", ACCOUNT_STATES)
def test_stored_roles_change_no_permission(boss, role, approved, active, eligible):
    user = make_user(username="member1", email="member1@example.com", role=role, staff_approved=approved,
                     is_active=active)
    _enroll(user, actor=boss)
    stored_org_role, stored_branch_roles = _roles(user)
    context = auth_context_for_user(user)
    # The context does not read the stored roles ...
    assert (context.organization_role, context.branch_role) == (None, None)
    assert context.is_eligible is eligible
    expected = COMPATIBILITY_GRANTS[role] if eligible else frozenset()
    assert permissions_for(context) == expected
    # ... and even placed in a context by hand they add nothing.
    with_roles = AuthContext(user_id=user.id, global_role=role, is_active=active, staff_approved=approved,
                             organization_role=stored_org_role, branch_role=stored_branch_roles[0])
    assert permissions_for(with_roles) == expected
