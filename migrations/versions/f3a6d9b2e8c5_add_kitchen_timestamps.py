"""add kitchen preparation timestamps to orders

Revision ID: f3a6d9b2e8c5
Revises: e5b8c1d4f7a2
Create Date: 2026-09-30 18:00:00.000000

Milestone 14. Two nullable columns, ``orders.preparing_at`` and
``orders.ready_at``, stamped by the order transition that moves an order to
PREPARING / READY. Existing orders keep NULL -- when they were prepared was
never recorded, and inventing it would be worse than leaving it blank. No
audit-event change (kitchen actions reuse ``order_status_changed``).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3a6d9b2e8c5'
down_revision = 'e5b8c1d4f7a2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('preparing_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('ready_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('ready_at')
        batch_op.drop_column('preparing_at')
