"""Organization membership and branch assignment (O2).

Two record types sit between a login account (``users``) and the local
organization/branch identity from O1 (``app/models/installation.py``):

- ``organization_memberships``: this account belongs to this organization's
  staff. One row per (organization, user) -- ``uq_organization_memberships_org_user``.
- ``branch_assignments``: this member is explicitly assigned to this branch.
  One row per (membership, branch) -- ``uq_branch_assignments_membership_branch``.

**Membership is not access.** Signing in and every permission check still
read only ``users.role``, ``users.is_active`` and ``users.staff_approved``
(``app/utils/authorization.py``). Revoking a membership or an assignment in
O2 changes no one's access; O3 is where roles and permissions start to
consult these rows.

**Lifecycle.** Each row is ``active`` or ``revoked`` and changes status in
place (``app/services/organization.py``); ``revoked_at``/``revoked_by_id``
describe the current revocation and the full history lives in the audit log
(``membership_*`` / ``branch_assignment_*`` events).
``ck_*_revoked_consistent`` keeps status and ``revoked_at`` in step.

**Cross-organization integrity is enforced by the database.** An assignment
carries its organization id, and two composite foreign keys tie it to both
``organization_memberships(id, organization_id)`` and
``branches(id, organization_id)``, so a member of one organization can never
be assigned to another organization's branch. The second key needs the
unique index ``uq_branches_id_organization`` on O1's ``branches`` table,
declared at the bottom of this module (O1's model file is unchanged).

Only ADMIN and STAFF accounts are members; customers never are (enforced in
the service layer -- a CHECK cannot look at ``users.role``).
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import enum_column
from app.models.installation import Branch, Organization
from app.models.user import User


class LifecycleStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


# The same text in the migration (4f1d8b2c6a90); tests/test_migrations.py
# compares the two.
_REVOKED_CONSISTENT = (
    "(status = 'active' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL)"
)


class OrganizationMembership(db.Model):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        sa.UniqueConstraint("organization_id", "user_id", name="uq_organization_memberships_org_user"),
        # Target of branch_assignments' composite foreign key.
        sa.UniqueConstraint("id", "organization_id", name="uq_organization_memberships_id_org"),
        sa.CheckConstraint(_REVOKED_CONSISTENT, name="ck_organization_memberships_revoked_consistent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[LifecycleStatus] = mapped_column(
        enum_column(LifecycleStatus, 16, name="ck_organization_memberships_status"),
        nullable=False, default=LifecycleStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    # NULL: created by the migration or a command-line path with no acting account.
    created_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    revoked_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    organization: Mapped[Organization] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    assignments: Mapped[list["BranchAssignment"]] = relationship(
        back_populates="membership", passive_deletes="all",
        primaryjoin="OrganizationMembership.id == foreign(BranchAssignment.membership_id)",
        order_by="BranchAssignment.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<OrganizationMembership id={self.id} user_id={self.user_id} {self.status.value}>"


class BranchAssignment(db.Model):
    __tablename__ = "branch_assignments"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["membership_id", "organization_id"],
            ["organization_memberships.id", "organization_memberships.organization_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "organization_id"], ["branches.id", "branches.organization_id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("membership_id", "branch_id", name="uq_branch_assignments_membership_branch"),
        sa.CheckConstraint(_REVOKED_CONSISTENT, name="ck_branch_assignments_revoked_consistent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Not a relationship of its own: present only so both composite foreign
    # keys can require the membership and the branch to share it.
    organization_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    membership_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[LifecycleStatus] = mapped_column(
        enum_column(LifecycleStatus, 16, name="ck_branch_assignments_status"),
        nullable=False, default=LifecycleStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    revoked_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    membership: Mapped[OrganizationMembership] = relationship(
        back_populates="assignments",
        primaryjoin="foreign(BranchAssignment.membership_id) == OrganizationMembership.id",
    )
    branch: Mapped[Branch] = relationship(primaryjoin="foreign(BranchAssignment.branch_id) == Branch.id")

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<BranchAssignment id={self.id} membership_id={self.membership_id} {self.status.value}>"


# Target of branch_assignments' (branch_id, organization_id) foreign key. On
# O1's table, created additively by migration 4f1d8b2c6a90 (a plain CREATE
# UNIQUE INDEX -- no table rebuild).
sa.Index("uq_branches_id_organization", Branch.__table__.c.id, Branch.__table__.c.organization_id, unique=True)
