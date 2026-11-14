"""Organization membership and branch assignment (O2): the only writer of
``organization_memberships`` and ``branch_assignments``.

**Membership is not access.** Nothing here touches ``users`` or is read by
``app/utils/authorization.py``: revoking a membership or an assignment does
not end anyone's session or change what they can do. Sign-in and
permissions still depend only on ``users.role``, ``is_active`` and
``staff_approved``. O3 is where roles and permissions consult these rows.

**Membership roles (O3.3).** Every row written here carries the role the
account's global role implies (``organization_role_for`` /
``branch_role_for``): an ADMIN membership is ``admin`` with NULL branch
roles on its assignments; a STAFF membership has no organization role and
its assignments are ``staff``. Reinstating keeps the stored role. Nothing
here writes ``owner`` or ``manager``, and the stored roles grant nothing.

Every change commits together with its audit rows (``membership_*`` /
``branch_assignment_*``), or not at all. Repeating a revoke or a reinstate
is a no-op that writes nothing. Revoking a membership also revokes its
active assignments in the same transaction; reinstating it restores none of
them -- each assignment is reinstated explicitly.

**Transaction contract -- the public functions own the session.**
``add_member``, ``assign_branch``, ``revoke_membership``,
``reinstate_membership``, ``revoke_assignment`` and
``reinstate_assignment`` act on the whole SQLAlchemy session transaction,
not on an isolated transaction limited to organization rows:

- a successful call commits *all* pending work in the session, the
  caller's unrelated changes included;
- a refusal or any other error rolls *all* pending work back;
- a no-op (revoking what is already revoked, reinstating what is already
  active) also rolls the session back.

Callers MUST therefore call them with no unrelated pending changes:
commit their own work first, or use a separate, clean session. This is the
same session-level behaviour as ``app/services/accounts.py::set_active`` and
``commit_with``; nothing enforces it yet.

``enroll_new_account`` is different: it is the helper the account-creation
paths (``create_admin``, ``invite_staff``, ``scripts/seed_demo.py``) call
inside their own transaction, and it never commits or rolls back -- the
caller commits the account, the membership, the assignment and their audit
rows together, or rolls all of them back. The read-only ``list_members``,
``check_consistency`` and ``local_organization_and_branch`` neither commit
nor roll back on success.

``actor`` is the acting account, or None for a command-line path with no
acting account (``flask create-admin``, ``scripts/seed_demo.py``). When
given it must be an active administrator. No route, form or command calls
the public functions yet (O2 adds no UI and no write command); the
account-creation paths call ``enroll_new_account``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.installation import Branch, Organization
from app.models.organization import (
    BranchAssignment, BranchRole, LifecycleStatus, OrganizationMembership, OrganizationRole)
from app.models.user import Role, User
from app.services import audit as audit_service
from app.services.errors import ValidationError
from app.services.installation import get_installation
from app.utils.clock import db_now

MEMBER_ROLES = (Role.ADMIN, Role.STAFF)
ACTIVE, REVOKED = LifecycleStatus.ACTIVE, LifecycleStatus.REVOKED


class OrganizationError(ValidationError):
    pass


def organization_role_for(role: Role) -> OrganizationRole | None:
    """The organization role a membership of an account with global role
    ``role`` carries (O3.3): ``admin`` for ADMIN, none for STAFF. The same
    rule migration 9a3e6c1d7b54 backfilled with."""
    return OrganizationRole.ADMIN if role == Role.ADMIN else None


def branch_role_for(role: Role) -> BranchRole | None:
    """The branch role an assignment of an account with global role
    ``role`` carries (O3.3): ``staff`` for STAFF, none for ADMIN -- no
    branch-manager role is inferred for administrators."""
    return BranchRole.STAFF if role == Role.STAFF else None


# --- helpers ------------------------------------------------------------------------------


def local_organization_and_branch() -> tuple[Organization, Branch]:
    """The installation's own organization and branch (O1). Raises
    ``InstallationIdentityError`` if the identity is missing or invalid --
    nothing is created or repaired."""
    identity = get_installation()
    organization = db.session.execute(
        sa.select(Organization).filter_by(public_id=identity.organization_public_id)).scalar_one()
    branch = db.session.execute(sa.select(Branch).filter_by(public_id=identity.branch_public_id)).scalar_one()
    return organization, branch


def _require_actor(actor: User | None) -> None:
    if actor is not None and (actor.role != Role.ADMIN or not actor.is_active):
        raise OrganizationError({"actor": ["Only an active administrator can change organization membership."]})


def _audit(event: AuditEvent, actor: User | None, **metadata) -> AuditLog:
    return audit_service.record_event(event, success=True, user_id=actor.id if actor else None, metadata=metadata)


def _commit(entries: list[AuditLog]) -> None:
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    mirror(entries)


def mirror(entries: list[AuditLog]) -> None:
    """Copy committed audit rows to the security log (see audit.mirror_to_security_log)."""
    for entry in entries:
        audit_service.mirror_to_security_log(entry)


def _locked(model, row_id: int):
    """Re-read one row under SELECT ... FOR UPDATE, so a concurrent change to
    the same row is serialised and seen (no lost update, no double audit)."""
    return db.session.execute(
        sa.select(model).where(model.id == row_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def _atomic(change):
    """Any exception -- a refusal, a failed audit write, a lost race -- rolls
    the whole transaction back before it propagates, so a half-made change
    (an assignment revoked without its membership, or a row without its
    audit event) can never be left pending in the session for a later commit
    to pick up."""
    @wraps(change)
    def wrapped(*args, **kwargs):
        try:
            return change(*args, **kwargs)
        except BaseException:
            db.session.rollback()
            raise
    return wrapped


def _refuse(field: str, message: str):
    db.session.rollback()
    raise OrganizationError({field: [message]})


# --- creating (no commit) ------------------------------------------------------------------


def _new_membership(organization: Organization, user: User, actor: User | None,
                    entries: list[AuditLog]) -> OrganizationMembership:
    if user.role not in MEMBER_ROLES:
        raise OrganizationError({"user": [
            f"{user.username} is a {user.role.value} account; only staff and administrator "
            "accounts can be organization members."]})
    existing = db.session.execute(sa.select(OrganizationMembership).filter_by(
        organization_id=organization.id, user_id=user.id)).scalar_one_or_none()
    if existing is not None:
        raise OrganizationError({"user": [
            f"{user.username} already has a {existing.status.value} membership; reinstate it instead."]})
    membership = OrganizationMembership(organization_id=organization.id, user_id=user.id, status=ACTIVE,
                                        organization_role=organization_role_for(user.role),
                                        created_by_id=actor.id if actor else None)
    db.session.add(membership)
    db.session.flush()
    entries.append(_audit(AuditEvent.MEMBERSHIP_CREATED, actor, membership_id=membership.id,
                          target_user_id=user.id, organization_id=organization.id))
    return membership


def _new_assignment(membership: OrganizationMembership, branch: Branch, actor: User | None,
                    entries: list[AuditLog]) -> BranchAssignment:
    if membership.status != ACTIVE:
        raise OrganizationError({"membership": ["A revoked membership cannot be assigned to a branch."]})
    if branch.organization_id != membership.organization_id:
        raise OrganizationError({"branch": ["That branch belongs to a different organization."]})
    existing = db.session.execute(sa.select(BranchAssignment).filter_by(
        membership_id=membership.id, branch_id=branch.id)).scalar_one_or_none()
    if existing is not None:
        raise OrganizationError({"branch": [
            f"This member already has a {existing.status.value} assignment to that branch; reinstate it instead."]})
    member = db.session.get(User, membership.user_id)
    assignment = BranchAssignment(organization_id=membership.organization_id, membership_id=membership.id,
                                  branch_id=branch.id, status=ACTIVE, branch_role=branch_role_for(member.role),
                                  created_by_id=actor.id if actor else None)
    db.session.add(assignment)
    db.session.flush()
    entries.append(_audit(AuditEvent.BRANCH_ASSIGNMENT_CREATED, actor, assignment_id=assignment.id,
                          membership_id=membership.id, target_user_id=membership.user_id, branch_id=branch.id))
    return assignment


def enroll_new_account(user: User, organization: Organization, branch: Branch, *,
                       actor: User | None = None) -> list[AuditLog]:
    """Add an active membership and an active assignment to ``branch`` for an
    account being created in the caller's transaction. Does not commit: the
    account, these rows and their audit rows commit together in the caller.
    Returns the audit rows, for ``mirror`` after that commit."""
    entries: list[AuditLog] = []
    membership = _new_membership(organization, user, actor, entries)
    _new_assignment(membership, branch, actor, entries)
    return entries


# --- public changes (each commits with its audit rows) ---------------------------------------


@_atomic
def add_member(user: User, *, actor: User | None = None) -> OrganizationMembership:
    """Make an existing staff or administrator account a member of this
    installation's organization. Adds no branch assignment."""
    _require_actor(actor)
    organization, _ = local_organization_and_branch()
    entries: list[AuditLog] = []
    try:
        membership = _new_membership(organization, user, actor, entries)
        _commit(entries)
    except OrganizationError:
        db.session.rollback()
        raise
    except IntegrityError:
        # Lost a race: a concurrent add_member for the same account committed
        # first, and uq_organization_memberships_org_user refused this one.
        db.session.rollback()
        raise OrganizationError({"user": [f"{user.username} already has a membership."]})
    return membership


@_atomic
def assign_branch(membership: OrganizationMembership, branch: Branch, *,
                  actor: User | None = None) -> BranchAssignment:
    """Assign an active member to a branch of the same organization."""
    _require_actor(actor)
    membership = _locked(OrganizationMembership, membership.id)
    entries: list[AuditLog] = []
    try:
        assignment = _new_assignment(membership, branch, actor, entries)
        _commit(entries)
    except OrganizationError:
        db.session.rollback()
        raise
    except IntegrityError:
        # Lost a race against a concurrent assignment to the same branch
        # (uq_branch_assignments_membership_branch).
        db.session.rollback()
        raise OrganizationError({"branch": ["This member already has an assignment to that branch."]})
    return assignment


@_atomic
def revoke_membership(membership: OrganizationMembership, *, actor: User | None = None) -> bool:
    """Revoke a membership and, in the same transaction, every active
    assignment under it. Returns False (and writes nothing) if it was
    already revoked. Does not affect sign-in or permissions."""
    _require_actor(actor)
    membership = _locked(OrganizationMembership, membership.id)
    if membership.status == REVOKED:
        db.session.rollback()
        return False
    now, actor_id, entries = db_now(), (actor.id if actor else None), []
    assignments = db.session.execute(
        sa.select(BranchAssignment).where(BranchAssignment.membership_id == membership.id)
        .order_by(BranchAssignment.id).with_for_update().execution_options(populate_existing=True)
    ).scalars().all()
    revoked_ids = []
    for assignment in assignments:
        if assignment.status == ACTIVE:
            assignment.status, assignment.revoked_at, assignment.revoked_by_id = REVOKED, now, actor_id
            revoked_ids.append(assignment.id)
            entries.append(_audit(AuditEvent.BRANCH_ASSIGNMENT_REVOKED, actor, assignment_id=assignment.id,
                                  membership_id=membership.id, target_user_id=membership.user_id,
                                  branch_id=assignment.branch_id, reason="membership_revoked"))
    membership.status, membership.revoked_at, membership.revoked_by_id = REVOKED, now, actor_id
    entries.append(_audit(AuditEvent.MEMBERSHIP_REVOKED, actor, membership_id=membership.id,
                          target_user_id=membership.user_id, revoked_assignment_ids=revoked_ids))
    _commit(entries)
    return True


@_atomic
def reinstate_membership(membership: OrganizationMembership, *, actor: User | None = None) -> bool:
    """Make a revoked membership active again. Its assignments stay as they
    are. Returns False (and writes nothing) if it was already active."""
    _require_actor(actor)
    membership = _locked(OrganizationMembership, membership.id)
    if membership.status == ACTIVE:
        db.session.rollback()
        return False
    user = db.session.get(User, membership.user_id)
    if user.role not in MEMBER_ROLES:
        _refuse("user", "Only staff and administrator accounts can be organization members.")
    membership.status, membership.revoked_at, membership.revoked_by_id = ACTIVE, None, None
    _commit([_audit(AuditEvent.MEMBERSHIP_REINSTATED, actor, membership_id=membership.id,
                    target_user_id=membership.user_id)])
    return True


@_atomic
def revoke_assignment(assignment: BranchAssignment, *, actor: User | None = None) -> bool:
    """Revoke one branch assignment. Returns False (and writes nothing) if it
    was already revoked."""
    _require_actor(actor)
    assignment = _locked(BranchAssignment, assignment.id)
    if assignment.status == REVOKED:
        db.session.rollback()
        return False
    actor_id = actor.id if actor else None
    assignment.status, assignment.revoked_at, assignment.revoked_by_id = REVOKED, db_now(), actor_id
    membership_user_id = db.session.scalar(
        sa.select(OrganizationMembership.user_id).where(OrganizationMembership.id == assignment.membership_id))
    _commit([_audit(AuditEvent.BRANCH_ASSIGNMENT_REVOKED, actor, assignment_id=assignment.id,
                    membership_id=assignment.membership_id, target_user_id=membership_user_id,
                    branch_id=assignment.branch_id)])
    return True


@_atomic
def reinstate_assignment(assignment: BranchAssignment, *, actor: User | None = None) -> bool:
    """Make a revoked assignment active again -- only under an active
    membership. Returns False (and writes nothing) if it was already active."""
    _require_actor(actor)
    # Membership first, then assignment: the same order revoke_membership locks in.
    membership = _locked(OrganizationMembership, assignment.membership_id)
    assignment = _locked(BranchAssignment, assignment.id)
    if membership.status != ACTIVE:
        _refuse("membership", "Reinstate the membership before reinstating its branch assignments.")
    if assignment.status == ACTIVE:
        db.session.rollback()
        return False
    assignment.status, assignment.revoked_at, assignment.revoked_by_id = ACTIVE, None, None
    _commit([_audit(AuditEvent.BRANCH_ASSIGNMENT_REINSTATED, actor, assignment_id=assignment.id,
                    membership_id=membership.id, target_user_id=membership.user_id,
                    branch_id=assignment.branch_id)])
    return True


# --- reading ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignmentView:
    assignment_id: int
    branch_id: int
    branch_name: str
    status: LifecycleStatus
    branch_role: BranchRole | None = None


@dataclass(frozen=True)
class MemberView:
    membership_id: int
    user_id: int
    username: str
    role: Role
    account_active: bool
    staff_approved: bool
    status: LifecycleStatus
    assignments: tuple[AssignmentView, ...]
    organization_role: OrganizationRole | None = None


@dataclass(frozen=True)
class ConsistencyIssue:
    kind: str
    message: str


def list_members() -> list[MemberView]:
    """Every membership of this installation's organization, with its account
    and assignments. Read-only."""
    organization, _ = local_organization_and_branch()
    memberships = db.session.execute(
        sa.select(OrganizationMembership).where(OrganizationMembership.organization_id == organization.id)
        .order_by(OrganizationMembership.id)).scalars().all()
    branch_names = dict(db.session.execute(sa.select(Branch.id, Branch.name)).all())
    views = []
    for m in memberships:
        user = db.session.get(User, m.user_id)
        assignments = tuple(
            AssignmentView(a.id, a.branch_id, branch_names.get(a.branch_id, "?"), a.status, a.branch_role)
            for a in db.session.execute(sa.select(BranchAssignment).where(
                BranchAssignment.membership_id == m.id).order_by(BranchAssignment.id)).scalars())
        views.append(MemberView(m.id, user.id, user.username, user.role, user.is_active, user.staff_approved,
                                m.status, assignments, m.organization_role))
    return views


def check_consistency() -> list[ConsistencyIssue]:
    """Missing or inconsistent relationships. Read-only; repairs nothing.

    - a staff or administrator account with no membership (e.g. inserted
      directly, bypassing the account-creation paths);
    - a membership held by a customer account;
    - an active membership with no branch assignment at all
      (``missing_assignment``), and -- since O3.3 -- a *revoked* membership
      with no assignment row either (``membership_without_assignment``):
      every creation path writes both rows and revocation deletes none;
    - an active assignment under a revoked membership;
    - (O3.3) a stored role that does not match the account's global role
      (``role_mismatch``), or an ``owner`` role, which nothing can create
      yet (``unexpected_owner``).

    An active membership whose assignments are all revoked is a legitimate
    state (they were revoked explicitly) and is not reported."""
    organization, _ = local_organization_and_branch()
    issues: list[ConsistencyIssue] = []

    member_user_ids = set(db.session.scalars(sa.select(OrganizationMembership.user_id).where(
        OrganizationMembership.organization_id == organization.id)))
    for user in db.session.execute(sa.select(User).where(User.role.in_(MEMBER_ROLES)).order_by(User.id)).scalars():
        if user.id not in member_user_ids:
            issues.append(ConsistencyIssue("missing_membership",
                                           f"{user.username} ({user.role.value}) has no organization membership."))

    for m in db.session.execute(sa.select(OrganizationMembership).order_by(OrganizationMembership.id)).scalars():
        user = db.session.get(User, m.user_id)
        if user.role not in MEMBER_ROLES:
            issues.append(ConsistencyIssue("customer_member",
                                           f"{user.username} is a {user.role.value} account but holds membership {m.id}."))
        assignments = list(db.session.execute(sa.select(BranchAssignment).where(
            BranchAssignment.membership_id == m.id).order_by(BranchAssignment.id)).scalars())
        statuses = [a.status for a in assignments]
        if m.status == ACTIVE and not statuses:
            issues.append(ConsistencyIssue("missing_assignment",
                                           f"{user.username}'s active membership has no branch assignment."))
        if m.status == REVOKED and not statuses:
            issues.append(ConsistencyIssue("membership_without_assignment",
                                           f"{user.username}'s revoked membership {m.id} has no branch assignment row."))
        if m.status == REVOKED and ACTIVE in statuses:
            issues.append(ConsistencyIssue("active_assignment_under_revoked_membership",
                                           f"{user.username}'s membership is revoked but an assignment is active."))
        if user.role in MEMBER_ROLES:
            issues.extend(_role_issues(user, m, assignments))
    return issues


def _role_issues(user: User, membership: OrganizationMembership,
                 assignments: list[BranchAssignment]) -> list[ConsistencyIssue]:
    issues = []
    if membership.organization_role == OrganizationRole.OWNER:
        issues.append(ConsistencyIssue("unexpected_owner",
                                       f"{user.username}'s membership {membership.id} holds the owner role, "
                                       "which nothing can assign yet."))
    elif membership.organization_role != organization_role_for(user.role):
        issues.append(ConsistencyIssue("role_mismatch",
                                       f"{user.username} ({user.role.value}): membership {membership.id} has "
                                       f"organization role {_label(membership.organization_role)}, expected "
                                       f"{_label(organization_role_for(user.role))}."))
    for a in assignments:
        if a.branch_role != branch_role_for(user.role):
            issues.append(ConsistencyIssue("role_mismatch",
                                           f"{user.username} ({user.role.value}): assignment {a.id} has branch role "
                                           f"{_label(a.branch_role)}, expected {_label(branch_role_for(user.role))}."))
    return issues


def _label(role) -> str:
    return "none" if role is None else role.value
