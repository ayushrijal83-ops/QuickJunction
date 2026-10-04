"""add admin_created audit event

Revision ID: a7c2e9d4b1f3
Revises: f3a6d9b2e8c5
Create Date: 2026-10-03 12:00:00.000000

M1 (production admin bootstrap). ``flask create-admin`` records an
``admin_created`` audit event (app/models/audit_log.py), so
``ck_audit_logs_event_type``'s allow-list is widened by that one value. No
table or column changes. Hand-written for the same reason as 38297b707b89.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'a7c2e9d4b1f3'
down_revision = 'f3a6d9b2e8c5'
branch_labels = None
depends_on = None


# The allow-list at f3a6d9b2e8c5, i.e. what downgrade() must restore.
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
)
_NEW_EVENTS = ('admin_created',)
_EVENTS_AFTER = _EVENTS_BEFORE + _NEW_EVENTS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade():
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_AFTER))


def downgrade():
    # Rows of the new type would violate the narrower constraint. The admin
    # accounts themselves are untouched; only their creation records go.
    op.execute("DELETE FROM audit_logs WHERE " + _in('event_type', _NEW_EVENTS))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_BEFORE))
