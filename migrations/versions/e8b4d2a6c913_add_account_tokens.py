"""add account tokens and account-security audit events

Revision ID: e8b4d2a6c913
Revises: a7c2e9d4b1f3
Create Date: 2026-10-04 12:00:00.000000

M4 (account security & staff onboarding). ``account_tokens`` holds the
SHA-256 digests of one-time staff-invitation and password-reset codes (never
the codes themselves); ``ck_audit_logs_event_type`` is widened by the eight
M4 events. No existing row is touched.

Written by hand for the reason recorded in 2d9f3b20045f. Downgrade uses
``drop_table`` alone -- dropping a foreign-key-backed index first fails on
MySQL (1553; see known issue #32).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e8b4d2a6c913'
down_revision = 'a7c2e9d4b1f3'
branch_labels = None
depends_on = None


# The allow-list at a7c2e9d4b1f3, i.e. what downgrade() must restore.
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
)
_NEW_EVENTS = (
    'password_changed', 'password_reset_issued', 'password_reset',
    'staff_invited', 'staff_invitation_accepted', 'account_token_rejected',
    'account_deactivated', 'account_activated',
)
_EVENTS_AFTER = _EVENTS_BEFORE + _NEW_EVENTS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade():
    op.create_table(
        'account_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(length=16), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(_in('purpose', ('staff_invite', 'password_reset')), name='ck_account_tokens_purpose'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_account_tokens_user_purpose', 'account_tokens', ['user_id', 'purpose'], unique=False)

    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_AFTER))


def downgrade():
    # Rows of the new types would violate the narrower constraint. Accounts
    # created by invitation are kept; only the codes and these records go.
    op.execute("DELETE FROM audit_logs WHERE " + _in('event_type', _NEW_EVENTS))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_BEFORE))
    op.drop_table('account_tokens')
