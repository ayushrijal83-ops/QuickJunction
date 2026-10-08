"""add restaurant profile

Revision ID: b1c4e7f9a2d5
Revises: e8b4d2a6c913
Create Date: 2026-10-08 18:00:00.000000

M5 (menu & restaurant foundation), part 1 of 2.

- ``restaurant_profile``: the installed restaurant's identity, one row
  (``ck_restaurant_profile_singleton``: id = 1). The row created here holds
  only name "Quick Junction" -- what every customer page already showed
  before M5, so the upgrade changes nothing customers see -- English as the
  default menu language, and no tagline, address, phone or email. It makes
  no claim about any restaurant; an admin enters the real details at
  /admin/settings/restaurant. Nothing references this table and no other
  table gains a column: this is not tenancy.
- ``ck_audit_logs_event_type`` widened by ``restaurant_profile_changed``.

No existing row is changed. Written by hand (reason in 2d9f3b20045f).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b1c4e7f9a2d5'
down_revision = 'e8b4d2a6c913'
branch_labels = None
depends_on = None


# The allow-list at e8b4d2a6c913, i.e. what downgrade() must restore.
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
)
_NEW_EVENTS = ('restaurant_profile_changed',)
_EVENTS_AFTER = _EVENTS_BEFORE + _NEW_EVENTS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade():
    profile = op.create_table(
        'restaurant_profile',
        # Not AUTO_INCREMENT: MySQL refuses a CHECK on one (3818), and the id is always 1.
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('tagline', sa.String(length=200), nullable=True),
        sa.Column('address', sa.String(length=255), nullable=True),
        sa.Column('phone', sa.String(length=32), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('default_locale', sa.String(length=8), server_default='en', nullable=False),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_restaurant_profile_singleton'),
        sa.CheckConstraint(_in('default_locale', ('en', 'ne')), name='ck_restaurant_profile_default_locale'),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.bulk_insert(profile, [{'id': 1, 'name': 'Quick Junction', 'default_locale': 'en'}])

    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_AFTER))


def downgrade():
    op.execute("DELETE FROM audit_logs WHERE " + _in('event_type', _NEW_EVENTS))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_BEFORE))
    op.drop_table('restaurant_profile')
