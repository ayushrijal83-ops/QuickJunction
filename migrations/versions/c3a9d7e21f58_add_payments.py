"""add payments

Revision ID: c3a9d7e21f58
Revises: b5e2f8c41a07
Create Date: 2026-09-29 16:00:00.000000

Milestone 11 (Payments). One ``payments`` row per order (UNIQUE order_id),
recorded by staff at the counter; refunds accumulate in ``refunded_amount``,
bounded by ``ck_payments_refund_within_amount``. No existing row is touched:
orders placed before this revision simply have no payment (reported as
"completed but unpaid" where applicable).

Written by hand for the reason recorded in 2d9f3b20045f. Downgrade uses
``drop_table`` alone -- dropping a foreign-key-backed index first fails on
MySQL (1553; see known issue #32).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3a9d7e21f58'
down_revision = 'b5e2f8c41a07'
branch_labels = None
depends_on = None


_EVENTS_BEFORE = (
    'register_success', 'login_success', 'login_failure', 'login_rate_limited', 'logout',
    'category_created', 'category_updated', 'category_deactivated',
    'menu_item_created', 'menu_item_updated', 'menu_item_availability_changed', 'ingredient_changed',
    'order_created', 'order_creation_failed',
    'order_status_changed', 'order_status_change_rejected',
    'staff_registered', 'staff_approved', 'staff_approval_revoked',
    'table_created', 'table_updated', 'table_status_changed',
    'reservation_created', 'reservation_status_changed',
)
_NEW_EVENTS = ('payment_recorded', 'payment_refunded')
_EVENTS_AFTER = _EVENTS_BEFORE + _NEW_EVENTS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade():
    op.create_table(
        'payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(length=16), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('refunded_amount', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.Column('captured_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('recorded_by_id', sa.Integer(), nullable=False),
        sa.Column('refunded_at', sa.DateTime(), nullable=True),
        sa.Column('refund_reason', sa.String(length=255), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint('amount > 0', name='ck_payments_amount_positive'),
        sa.CheckConstraint(
            'refunded_amount >= 0 AND refunded_amount <= amount', name='ck_payments_refund_within_amount'
        ),
        sa.CheckConstraint(_in('method', ('cash', 'card', 'wallet')), name='ck_payments_method'),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['recorded_by_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_id'),
    )

    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_AFTER))


def downgrade():
    op.execute("DELETE FROM audit_logs WHERE " + _in('event_type', _NEW_EVENTS))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_BEFORE))

    op.drop_table('payments')
