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

**Membership roles (O3.3).** ``organization_memberships.organization_role``
and ``branch_assignments.branch_role`` store the approved O3 roles
(``OrganizationRole``, ``BranchRole``), derived from the account's global
role by a fixed rule: an ADMIN's membership is ``admin`` and its assignments
carry no branch role; a STAFF member's membership carries no organization
role and its assignments are ``staff``. Nothing writes ``owner`` or
``manager`` yet. **Stored roles grant nothing** in O3.3: the grant maps in
``app/utils/permissions.py`` are empty and no permission check reads these
columns. Eligibility is always computed from the live account
(``is_active``, and ``staff_approved`` for STAFF only).

Every membership, of any status and either role, has at least one
assignment row to the local branch: every creation path writes both
(``enroll_new_account``, the O2 backfill), revocation never deletes rows,
and ``check_consistency`` reports a membership without one.
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


class OrganizationRole(str, enum.Enum):
    """The approved O3 organization roles. Defined here, beside the column
    that stores them; ``app/utils/permissions.py`` re-exports it."""

    OWNER = "owner"   # ORGANIZATION_OWNER -- never written in O3.3 (a later, CLI-only milestone)
    ADMIN = "admin"   # ORGANIZATION_ADMIN


class BranchRole(str, enum.Enum):
    """The approved O3 branch roles; re-exported by ``app/utils/permissions.py``."""

    MANAGER = "manager"   # BRANCH_MANAGER -- never written in O3.3
    STAFF = "staff"       # BRANCH_STAFF


# The same text in the migration (4f1d8b2c6a90); tests/test_migrations.py
# compares the two.
_REVOKED_CONSISTENT = (
    "(status = 'active' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL)"
)


# --- exact-value role CHECKs (O3.3) -------------------------------------------------------------
#
# The role columns accept only the exact canonical lowercase values. A plain
# ``col IN ('owner', 'admin')`` is exact on SQLite (its default BINARY
# collation compares bytes), but on MySQL it compares with the column's
# collation -- utf8mb4_unicode_ci in production (scripts/db_setup.py),
# utf8mb4_0900_ai_ci on a default MySQL 8 server -- which is case- and
# accent-insensitive (and PAD SPACE for unicode_ci), so 'Admin', 'ADMIN',
# 'ádmin' and 'admin ' would pass. On MySQL the CHECK therefore compares the
# value's bytes: ``CAST(col AS BINARY)`` is a binary string, and a comparison
# with a binary string is byte by byte -- case-, accent- and trailing-space-
# sensitive -- whatever the column's character set or collation.
#
# The two texts are dialect-specific on purpose. Each CheckConstraint below
# carries ``ddl_if(dialect=...)``, so ``create_all`` emits exactly one of them
# per database, under the same name, and migration 9a3e6c1d7b54 adds the same
# text for the same dialect (tests/test_membership_roles.py and
# tests/test_migrations.py compare the two). Only SQLite and MySQL are
# supported, as everywhere else in the schema.

ROLE_CHECK_DIALECTS = ("sqlite", "mysql")


def role_check_sql(column: str, values, dialect: str) -> str:
    """The exact-value CHECK text for a role column on ``dialect``."""
    listed = ", ".join(f"'{value}'" for value in values)
    if dialect == "mysql":
        return f"CAST({column} AS BINARY) IN ({listed})"
    if dialect == "sqlite":
        return f"{column} IN ({listed})"
    raise ValueError(f"unsupported dialect for role CHECKs: {dialect!r}")


def _role_checks(column: str, enum_cls: type[enum.Enum], name: str) -> tuple[sa.CheckConstraint, ...]:
    values = [member.value for member in enum_cls]
    return tuple(
        sa.CheckConstraint(role_check_sql(column, values, dialect), name=name).ddl_if(dialect=dialect)
        for dialect in ROLE_CHECK_DIALECTS
    )


def _role_type(enum_cls: type[enum.Enum]) -> sa.Enum:
    """Same storage and value mapping as ``enum_column``, but without its
    generic CHECK: the exact-value CHECKs above replace it."""
    return sa.Enum(enum_cls, native_enum=False, create_constraint=False, length=16, validate_strings=True,
                   values_callable=lambda cls: [member.value for member in cls])


class OrganizationMembership(db.Model):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        sa.UniqueConstraint("organization_id", "user_id", name="uq_organization_memberships_org_user"),
        # Target of branch_assignments' composite foreign key.
        sa.UniqueConstraint("id", "organization_id", name="uq_organization_memberships_id_org"),
        sa.CheckConstraint(_REVOKED_CONSISTENT, name="ck_organization_memberships_revoked_consistent"),
        *_role_checks("organization_role", OrganizationRole, "ck_organization_memberships_organization_role"),
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
    # O3.3. ``admin`` for an ADMIN account, NULL for STAFF. Added by migration
    # 9a3e6c1d7b54 as a plain ADD COLUMN with the exact-value CHECK declared in
    # __table_args__ (dialect-specific text; see role_check_sql).
    organization_role: Mapped[OrganizationRole | None] = mapped_column(_role_type(OrganizationRole), nullable=True)
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
        *_role_checks("branch_role", BranchRole, "ck_branch_assignments_branch_role"),
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
    # O3.3. ``staff`` when the member is a STAFF account, NULL for ADMIN (an
    # ADMIN's authority is organization-wide; no branch-manager role is
    # inferred). Added by migration 9a3e6c1d7b54, like organization_role, with
    # the exact-value CHECK declared in __table_args__.
    branch_role: Mapped[BranchRole | None] = mapped_column(_role_type(BranchRole), nullable=True)
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
