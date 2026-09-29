"""add tax and discount pricing

Revision ID: d7f1e3a9c2b4
Revises: c3a9d7e21f58
Create Date: 2026-09-30 10:00:00.000000

Milestone 12.

- ``pricing_settings``: one row (id 1) seeded with the application defaults
  13.00 % tax and 20.00 % staff maximum discount (see
  app/models/pricing_settings.py; an admin changes them at /admin/settings).
- ``orders``: pricing snapshot columns. Existing orders are backfilled with
  discount 0 and tax rate/amount 0 via server defaults -- exactly what they
  were charged (``total == subtotal`` before M12) -- so the new
  ``ck_orders_total_formula`` holds for every existing row.
- Audit allow-list widened for the two pricing events.

Written by hand (reason in 2d9f3b20045f). Downgrade drops foreign keys
before anything that depends on them (MySQL 1553; see known issue #32).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7f1e3a9c2b4'
down_revision = 'c3a9d7e21f58'
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
    'payment_recorded', 'payment_refunded',
)
_NEW_EVENTS = ('order_discount_applied', 'pricing_settings_changed')
_EVENTS_AFTER = _EVENTS_BEFORE + _NEW_EVENTS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade():
    settings = op.create_table(
        'pricing_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('staff_max_discount', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint('tax_rate >= 0 AND tax_rate <= 50.00', name='ck_pricing_settings_tax_rate_range'),
        sa.CheckConstraint(
            'staff_max_discount >= 0 AND staff_max_discount <= 100', name='ck_pricing_settings_staff_max_range'
        ),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.bulk_insert(settings, [{'id': 1, 'tax_rate': 13.00, 'staff_max_discount': 20.00}])

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('discount_type', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('discount_value', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(
            sa.Column('discount_amount', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False)
        )
        batch_op.add_column(sa.Column('discount_reason', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('discounted_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), server_default='0', nullable=False))
        batch_op.add_column(
            sa.Column('tax_amount', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False)
        )
        batch_op.create_foreign_key(
            'fk_orders_discounted_by_id_users', 'users', ['discounted_by_id'], ['id'], ondelete='RESTRICT'
        )
        batch_op.create_check_constraint('ck_orders_discount_type', _in('discount_type', ('percent', 'fixed')))
        batch_op.create_check_constraint(
            'ck_orders_discount_within_subtotal', 'discount_amount >= 0 AND discount_amount <= subtotal'
        )
        batch_op.create_check_constraint('ck_orders_tax_nonnegative', 'tax_amount >= 0')
        batch_op.create_check_constraint(
            'ck_orders_total_formula', 'ABS(total - (subtotal - discount_amount + tax_amount)) < 0.005'
        )

    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_AFTER))


def downgrade():
    op.execute("DELETE FROM audit_logs WHERE " + _in('event_type', _NEW_EVENTS))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_BEFORE))

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_constraint('fk_orders_discounted_by_id_users', type_='foreignkey')
        batch_op.drop_constraint('ck_orders_total_formula', type_='check')
        batch_op.drop_constraint('ck_orders_tax_nonnegative', type_='check')
        batch_op.drop_constraint('ck_orders_discount_within_subtotal', type_='check')
        batch_op.drop_constraint('ck_orders_discount_type', type_='check')
        batch_op.drop_column('tax_amount')
        batch_op.drop_column('tax_rate')
        batch_op.drop_column('discounted_by_id')
        batch_op.drop_column('discount_reason')
        batch_op.drop_column('discount_amount')
        batch_op.drop_column('discount_value')
        batch_op.drop_column('discount_type')

    op.drop_table('pricing_settings')
