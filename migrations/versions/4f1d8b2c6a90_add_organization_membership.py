"""add organization membership and branch assignment

Revision ID: 4f1d8b2c6a90
Revises: 4c7e2a9f1b38
Create Date: 2026-11-13 15:00:00.000000

O2 (organization membership and staff-to-branch assignment). Additive:

- ``organization_memberships`` and ``branch_assignments`` -- see
  app/models/organization.py. Each row is ``active`` or ``revoked``.
- ``uq_branches_id_organization``: a unique index on O1's ``branches``
  (id, organization_id), the target of branch_assignments' composite foreign
  key. A plain CREATE UNIQUE INDEX -- the table is not rebuilt.
- ``ck_audit_logs_event_type`` widened by the six ``membership_*`` /
  ``branch_assignment_*`` events.

Backfill, by a fixed rule and nothing else: every ``admin`` and ``staff``
account -- active or not, approved or pending, invited or not -- gets one
active membership in the installation's organization and one active
assignment to its branch, with ``created_by_id`` NULL. ``customer`` accounts
get neither. No account field, role, password, id, session version or
relationship changes, no owner is chosen, and no audit row is written.
Membership grants no access: sign-in and permissions still read only
``users.role``, ``is_active`` and ``staff_approved``.

The organization and branch are the ones O1 (4c7e2a9f1b38) created. If the
database does not hold exactly one organization and one branch of it, the
upgrade stops before changing anything rather than guess.

downgrade() deletes the O2 audit events, restores the previous allow-list
and drops the two tables and the index -- and with them every membership,
assignment and its revocation history. Upgrading again backfills fresh rows
(new ids, all active). Take and verify a backup before downgrading. Written
by hand (reason in 2d9f3b20045f).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4f1d8b2c6a90'
down_revision = '4c7e2a9f1b38'
branch_labels = None
depends_on = None


class MembershipBackfillRefused(RuntimeError):
    """The database does not hold exactly one organization and one branch."""


# The allow-list at 4c7e2a9f1b38 (unchanged since b1c4e7f9a2d5), i.e. what
# downgrade() must restore.
_EVENTS_BEFORE = (
    'register_success', 'login_success', 'login_failure', 'login_rate_limited', 'logout',
    'category_created', 'category_updated', 'category_deactivated',
    'menu_item_created', 'menu_item_updated', 'menu_item_availability_changed', 'ingredient_changed',
    'order_created', 'order_creation_failed', 'order_status_changed', 'order_status_change_rejected',
    'staff_registered', 'staff_approved', 'staff_approval_revoked',
    'table_created', 'table_updated', 'table_status_changed',
    'reservation_created', 'reservation_status_changed',
    'payment_recorded', 'payment_refunded',
    'order_discount_applied', 'pricing_settings_changed',
    'stock_item_saved', 'stock_movement_recorded', 'recipe_updated',
    'admin_created',
    'password_changed', 'password_reset_issued', 'password_reset',
    'staff_invited', 'staff_invitation_accepted', 'account_token_rejected',
    'account_deactivated', 'account_activated',
    'restaurant_profile_changed',
)
_NEW_EVENTS = (
    'membership_created', 'membership_revoked', 'membership_reinstated',
    'branch_assignment_created', 'branch_assignment_revoked', 'branch_assignment_reinstated',
)
_EVENTS_AFTER = _EVENTS_BEFORE + _NEW_EVENTS

_STATUSES = ('active', 'revoked')
_REVOKED_CONSISTENT = (
    "(status = 'active' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL)"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _local_organization_and_branch(conn) -> tuple[int, int]:
    organizations = conn.execute(sa.text("SELECT id FROM organizations")).scalars().all()
    branches = conn.execute(sa.text("SELECT id, organization_id FROM branches")).all()
    if len(organizations) != 1 or len(branches) != 1 or branches[0][1] != organizations[0]:
        raise MembershipBackfillRefused(
            f"O2 membership backfill needs exactly one organization and one branch of it; found "
            f"{len(organizations)} organization(s) and {len(branches)} branch(es). Nothing was changed. "
            "Check the installation identity (flask installation show) and restore a backup if it is damaged.")
    return organizations[0], branches[0][0]


def _lifecycle_columns():
    return [
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_by_id', sa.Integer(), nullable=True),
    ]


def upgrade():
    conn = op.get_bind()
    # Checked before any DDL: MySQL commits each CREATE TABLE on its own, so
    # refusing afterwards would leave half an upgrade behind.
    organization_id, branch_id = _local_organization_and_branch(conn)

    op.create_index('uq_branches_id_organization', 'branches', ['id', 'organization_id'], unique=True)

    op.create_table(
        'organization_memberships',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        *_lifecycle_columns(),
        sa.CheckConstraint(_in('status', _STATUSES), name='ck_organization_memberships_status'),
        sa.CheckConstraint(_REVOKED_CONSISTENT, name='ck_organization_memberships_revoked_consistent'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['revoked_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'user_id', name='uq_organization_memberships_org_user'),
        sa.UniqueConstraint('id', 'organization_id', name='uq_organization_memberships_id_org'),
    )
    op.create_table(
        'branch_assignments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('membership_id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=False),
        *_lifecycle_columns(),
        sa.CheckConstraint(_in('status', _STATUSES), name='ck_branch_assignments_status'),
        sa.CheckConstraint(_REVOKED_CONSISTENT, name='ck_branch_assignments_revoked_consistent'),
        sa.ForeignKeyConstraint(['membership_id', 'organization_id'],
                                ['organization_memberships.id', 'organization_memberships.organization_id'],
                                ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['branch_id', 'organization_id'], ['branches.id', 'branches.organization_id'],
                                ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['revoked_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('membership_id', 'branch_id', name='uq_branch_assignments_membership_branch'),
    )

    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_AFTER))

    # Backfill: every admin and staff account, whatever its activation or
    # approval state; never a customer. Ordered by user id so membership ids
    # follow account ids.
    conn.execute(sa.text(
        "INSERT INTO organization_memberships (organization_id, user_id, status) "
        "SELECT :o, id, 'active' FROM users WHERE role IN ('admin', 'staff') ORDER BY id"), {'o': organization_id})
    conn.execute(sa.text(
        "INSERT INTO branch_assignments (organization_id, membership_id, branch_id, status) "
        "SELECT :o, id, :b, 'active' FROM organization_memberships ORDER BY id"),
        {'o': organization_id, 'b': branch_id})


def downgrade():
    op.execute("DELETE FROM audit_logs WHERE " + _in('event_type', _NEW_EVENTS))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_BEFORE))
    op.drop_table('branch_assignments')
    op.drop_table('organization_memberships')
    op.drop_index('uq_branches_id_organization', table_name='branches')
