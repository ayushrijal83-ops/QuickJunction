"""add inventory: stock fields, recipe quantities, stock movements

Revision ID: e5b8c1d4f7a2
Revises: d7f1e3a9c2b4
Create Date: 2026-09-30 14:00:00.000000

Milestone 13. Extends the existing ``ingredients`` / ``menu_item_ingredients``
tables instead of adding a parallel inventory model.

- ``ingredients``: unit (default 'piece'), current/minimum quantity (default
  0), is_active (default true), updated_at. Existing ingredients start at
  quantity 0 -- nothing is invented; opening stock is entered as a PURCHASE
  movement so it is on the ledger.
- ``menu_item_ingredients.quantity`` (default 0): the recipe amount per
  portion. Existing links become quantity 0 = "listed, not stock-tracked",
  so no existing order or menu behaviour changes until an admin sets recipes.
- ``stock_movements``: the ledger, with UNIQUE (order_id, ingredient_id) for
  idempotent sale deductions.
- Audit allow-list widened for the three inventory events.

Written by hand (reason in 2d9f3b20045f). Downgrade: drop_table and column
drops only -- no FK-backed index is dropped on its own (MySQL 1553, #32).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5b8c1d4f7a2'
down_revision = 'd7f1e3a9c2b4'
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
    'order_discount_applied', 'pricing_settings_changed',
)
_NEW_EVENTS = ('stock_item_saved', 'stock_movement_recorded', 'recipe_updated')
_EVENTS_AFTER = _EVENTS_BEFORE + _NEW_EVENTS
_QTY = sa.Numeric(precision=12, scale=3)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade():
    with op.batch_alter_table('ingredients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('unit', sa.String(length=16), server_default='piece', nullable=False))
        batch_op.add_column(sa.Column('current_quantity', _QTY, server_default='0', nullable=False))
        batch_op.add_column(sa.Column('minimum_quantity', _QTY, server_default='0', nullable=False))
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False)
        )
        batch_op.create_check_constraint('ck_ingredients_unit', _in('unit', ('g', 'kg', 'ml', 'l', 'piece')))
        batch_op.create_check_constraint('ck_ingredients_minimum_nonnegative', 'minimum_quantity >= 0')

    with op.batch_alter_table('menu_item_ingredients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('quantity', _QTY, server_default='0', nullable=False))
        batch_op.create_check_constraint('ck_menu_item_ingredients_quantity_nonnegative', 'quantity >= 0')

    op.create_table(
        'stock_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ingredient_id', sa.Integer(), nullable=False),
        sa.Column('movement_type', sa.String(length=16), nullable=False),
        sa.Column('quantity_change', _QTY, nullable=False),
        sa.Column('quantity_after', _QTY, nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint('quantity_change <> 0', name='ck_stock_movements_change_nonzero'),
        sa.CheckConstraint(
            "(movement_type = 'sale' AND order_id IS NOT NULL) OR (movement_type <> 'sale' AND order_id IS NULL)",
            name='ck_stock_movements_sale_has_order',
        ),
        sa.CheckConstraint(
            _in('movement_type', ('purchase', 'restock', 'sale', 'waste', 'adjustment')),
            name='ck_stock_movements_movement_type',
        ),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['ingredient_id'], ['ingredients.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_id', 'ingredient_id', name='uq_stock_movements_order_ingredient'),
    )
    with op.batch_alter_table('stock_movements', schema=None) as batch_op:
        batch_op.create_index('ix_stock_movements_ingredient_created', ['ingredient_id', 'created_at'], unique=False)

    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_AFTER))


def downgrade():
    op.execute("DELETE FROM audit_logs WHERE " + _in('event_type', _NEW_EVENTS))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_audit_logs_event_type', type_='check')
        batch_op.create_check_constraint('ck_audit_logs_event_type', _in('event_type', _EVENTS_BEFORE))

    op.drop_table('stock_movements')

    with op.batch_alter_table('menu_item_ingredients', schema=None) as batch_op:
        batch_op.drop_constraint('ck_menu_item_ingredients_quantity_nonnegative', type_='check')
        batch_op.drop_column('quantity')

    with op.batch_alter_table('ingredients', schema=None) as batch_op:
        batch_op.drop_constraint('ck_ingredients_minimum_nonnegative', type_='check')
        batch_op.drop_constraint('ck_ingredients_unit', type_='check')
        batch_op.drop_column('updated_at')
        batch_op.drop_column('is_active')
        batch_op.drop_column('minimum_quantity')
        batch_op.drop_column('current_quantity')
        batch_op.drop_column('unit')
