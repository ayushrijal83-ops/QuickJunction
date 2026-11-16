"""Membership-aware authorization context (O3.4).

``resolve_auth_context(user)`` builds the ``AuthContext`` for an account from
the database as it is now: the account row, this installation's local
organization and branch (O1), and the account's organization membership and
branch assignment with their stored roles (O2, O3.3).
``current_resolved_auth_context()`` does the same for the signed-in user of
the request, through ``get_current_user()`` and therefore through its
session, deactivation, pending-staff and session-version rules, unchanged.

**Context resolution only.** Nothing calls this module yet. It grants and
revokes nothing: ``COMPATIBILITY_GRANTS``, ``permissions_for()``,
``has_permission()``, the empty role grant maps, every decorator, route and
service, and ``auth_context_for_user()`` / ``current_auth_context()``
(still membership-blind, as tests/test_permissions.py pins) are unchanged.

Policy (approved for O3.4):

- **Customers and ineligible accounts** (inactive accounts; STAFF awaiting
  approval) get the existing global-only context from
  ``auth_context_for_user()``, which holds no permission, and no workforce
  lookup is made.
- **Eligible workforce accounts** -- an active ADMIN (whatever
  ``staff_approved`` says) or an active, approved STAFF -- are resolved in
  full, and every state that cannot be trusted **fails closed** with
  ``WorkforceContextError`` rather than a context: a context for an eligible
  account would still carry its global-role compatibility grants, which
  would be a silent fallback. The account must hold exactly one membership,
  in the local organization, ``active``, with the organization role its
  global role implies (``admin`` for ADMIN, none for STAFF; ``owner`` is
  refused -- no owner can exist yet); and that membership exactly one
  assignment, to the local branch of the same organization, ``active``, with
  the implied branch role (``staff`` for STAFF, none for ADMIN).

**Trust boundary.** The organization, branch and roles come only from
database rows found by the account's id. Nothing is read from request
parameters, bodies, headers, cookies or the session, and the result is
never stored anywhere (no ``g``, no session, no cache): every call reads the
current state.

**Read-only and transaction-neutral.** Two column-only SELECTs through the
caller's session, inside ``no_autoflush`` -- as is reading the account row
itself, since reloading an expired attribute would otherwise flush the
caller's pending changes first: the caller's pending changes are never
flushed, no ORM object enters the identity map, nothing is written,
and this module never commits or rolls back -- unlike ``get_installation()``,
which rolls the session back on a database error and is therefore not used
here. The caller (or the request teardown) keeps ownership of the
transaction. A database error becomes ``WorkforceContextError`` naming only
the exception class, never the driver message (which can embed connection
details).
"""

from __future__ import annotations

import dataclasses

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.installation import INSTALLATION_ID
from app.models.organization import BranchRole, OrganizationRole
from app.models.user import Role, User
from app.utils.authorization import get_current_user
from app.utils.permissions import AuthContext, auth_context_for_user

WORKFORCE_ROLES = (Role.ADMIN, Role.STAFF)
ACTIVE = "active"

# The organization and branch role each global role must hold (O3.3's fixed
# rule; app/services/organization.py organization_role_for / branch_role_for).
_EXPECTED_ORGANIZATION_ROLE = {Role.ADMIN: OrganizationRole.ADMIN.value, Role.STAFF: None}
_EXPECTED_BRANCH_ROLE = {Role.ADMIN: None, Role.STAFF: BranchRole.STAFF.value}

# Q1: the installation identity's structure -- the O1/O2 rule: exactly one
# installation row (id 1), one organization and one branch, correctly linked.
_IDENTITY_SQL = sa.text(
    "SELECT i.id AS installation_id, b.id AS branch_id, b.organization_id AS branch_organization_id, "
    "o.id AS organization_id, "
    "(SELECT COUNT(*) FROM installation) AS installations, "
    "(SELECT COUNT(*) FROM organizations) AS organizations, "
    "(SELECT COUNT(*) FROM branches) AS branches "
    "FROM installation i "
    "LEFT JOIN branches b ON b.id = i.branch_id "
    "LEFT JOIN organizations o ON o.id = b.organization_id")

# Q2: every membership the account holds, in any organization, with every
# assignment under it -- so duplicates and wrong organizations or branches
# are seen, not filtered out.
_MEMBERSHIP_SQL = sa.text(
    "SELECT m.id AS membership_id, m.organization_id AS membership_organization_id, "
    "m.status AS membership_status, m.organization_role AS organization_role, "
    "a.id AS assignment_id, a.organization_id AS assignment_organization_id, a.branch_id AS branch_id, "
    "a.status AS assignment_status, a.branch_role AS branch_role "
    "FROM organization_memberships m "
    "LEFT JOIN branch_assignments a ON a.membership_id = m.id "
    "WHERE m.user_id = :user_id "
    "ORDER BY m.id, a.id")


class WorkforceContextError(RuntimeError):
    """An eligible workforce account's organization context cannot be
    established safely. ``code`` names the reason; the message is for
    operators (ids only, no driver detail) and is not meant for clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def requires_workforce_resolution(context: AuthContext) -> bool:
    """Only eligible ADMIN and STAFF accounts are resolved; customers and
    ineligible accounts keep the global-only context."""
    return context.global_role in WORKFORCE_ROLES and context.is_eligible


def resolve_auth_context(user: User) -> AuthContext:
    """The authorization context for ``user`` from the current database
    state. Raises ``WorkforceContextError`` for an eligible workforce account
    whose organization context is missing, revoked, inconsistent or
    unreadable -- and for any account whose row cannot be read
    (``database_error``); never writes, flushes, commits or rolls back."""
    try:
        # Everything inside no_autoflush, including reading ``user``: an
        # expired attribute is reloaded from the database, and that reload
        # would otherwise flush the caller's pending changes first.
        with db.session.no_autoflush:
            base = auth_context_for_user(user)
            if not requires_workforce_resolution(base):
                return base
            identity_rows = db.session.execute(_IDENTITY_SQL).all()
            membership_rows = db.session.execute(_MEMBERSHIP_SQL, {"user_id": base.user_id}).all()
    except SQLAlchemyError as exc:
        raise WorkforceContextError(
            "database_error",
            f"The authorization context could not be read ({type(exc).__name__}).") from exc
    organization_id, branch_id = local_organization_and_branch_ids(identity_rows)
    return workforce_context(base, organization_id, branch_id, membership_rows)


def current_resolved_auth_context() -> AuthContext | None:
    """The resolved context for the signed-in user of this request, or
    ``None`` when there is none (anonymous, or a session ``get_current_user``
    refuses). Session handling is exactly ``get_current_user()``'s."""
    user = get_current_user()
    return None if user is None else resolve_auth_context(user)


# --- the rules, as pure functions over Q1 / Q2 rows ----------------------------------------------


def local_organization_and_branch_ids(identity_rows) -> tuple[int, int]:
    """Q1's rows -> (organization id, branch id), or ``WorkforceContextError``."""
    if not identity_rows:
        raise WorkforceContextError("missing_identity", "No installation identity found.")
    row = identity_rows[0]
    if (len(identity_rows) != 1 or row.installations != 1 or row.installation_id != INSTALLATION_ID
            or row.organizations != 1 or row.branches != 1):
        raise WorkforceContextError(
            "invalid_identity",
            f"The installation identity is invalid: {row.installations} installation row(s), "
            f"{row.organizations} organization(s), {row.branches} branch(es); exactly one of each is required.")
    if row.branch_id is None or row.organization_id is None or row.branch_organization_id != row.organization_id:
        raise WorkforceContextError(
            "invalid_identity", "The installation identity's branch and organization links are broken.")
    return row.organization_id, row.branch_id


def workforce_context(base: AuthContext, organization_id: int, branch_id: int, membership_rows) -> AuthContext:
    """Q2's rows for an eligible workforce account -> its full context, or
    ``WorkforceContextError``. Nothing is repaired or defaulted."""
    user = f"user {base.user_id} ({base.global_role.value})"
    membership_ids = sorted({row.membership_id for row in membership_rows})
    if not membership_ids:
        raise WorkforceContextError("missing_membership", f"{user} has no organization membership.")
    if len(membership_ids) != 1:
        raise WorkforceContextError(
            "ambiguous_membership", f"{user} has {len(membership_ids)} memberships {membership_ids}; exactly one "
                                    "is required.")
    membership = membership_rows[0]
    if membership.membership_organization_id != organization_id:
        raise WorkforceContextError(
            "wrong_organization", f"{user}'s membership {membership.membership_id} is in organization "
                                  f"{membership.membership_organization_id}, not this installation's.")
    if membership.membership_status != ACTIVE:
        raise WorkforceContextError(
            "revoked_membership", f"{user}'s membership {membership.membership_id} is "
                                  f"{membership.membership_status}, not active.")
    organization_role = membership.organization_role
    if organization_role == OrganizationRole.OWNER.value:
        raise WorkforceContextError(
            "unexpected_owner", f"{user}'s membership {membership.membership_id} holds the owner role, which "
                                "nothing can assign yet.")
    if organization_role != _EXPECTED_ORGANIZATION_ROLE[base.global_role]:
        raise WorkforceContextError(
            "role_mismatch", f"{user}'s membership {membership.membership_id} has organization role "
                             f"{organization_role!r}, expected {_EXPECTED_ORGANIZATION_ROLE[base.global_role]!r}.")

    assignments = [row for row in membership_rows if row.assignment_id is not None]
    if not assignments:
        raise WorkforceContextError(
            "missing_assignment", f"{user}'s membership {membership.membership_id} has no branch assignment.")
    for assignment in assignments:
        if assignment.assignment_organization_id != membership.membership_organization_id:
            raise WorkforceContextError(
                "wrong_organization", f"{user}'s assignment {assignment.assignment_id} is in organization "
                                      f"{assignment.assignment_organization_id}, not its membership's.")
        if assignment.branch_id != branch_id:
            raise WorkforceContextError(
                "wrong_branch", f"{user}'s assignment {assignment.assignment_id} is to branch "
                                f"{assignment.branch_id}, not this installation's.")
    if len(assignments) != 1:
        raise WorkforceContextError(
            "ambiguous_assignment", f"{user}'s membership {membership.membership_id} has {len(assignments)} "
                                    "assignments; exactly one is required.")
    assignment = assignments[0]
    if assignment.assignment_status != ACTIVE:
        raise WorkforceContextError(
            "revoked_assignment", f"{user}'s assignment {assignment.assignment_id} is "
                                  f"{assignment.assignment_status}, not active.")
    if assignment.branch_role != _EXPECTED_BRANCH_ROLE[base.global_role]:
        raise WorkforceContextError(
            "role_mismatch", f"{user}'s assignment {assignment.assignment_id} has branch role "
                             f"{assignment.branch_role!r}, expected {_EXPECTED_BRANCH_ROLE[base.global_role]!r}.")

    return dataclasses.replace(
        base,
        organization_id=organization_id,
        branch_id=branch_id,
        organization_role=OrganizationRole(organization_role) if organization_role is not None else None,
        branch_role=BranchRole(assignment.branch_role) if assignment.branch_role is not None else None,
    )
