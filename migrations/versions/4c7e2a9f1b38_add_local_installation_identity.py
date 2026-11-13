"""add local installation identity

Revision ID: 4c7e2a9f1b38
Revises: e39e6dffcb5d
Create Date: 2026-11-13 12:00:00.000000

O1 (local organization & branch identity). Purely additive:

- ``organizations``, ``branches`` and ``installation`` (one row, id = 1,
  ``ck_installation_singleton``) -- see app/models/installation.py.
- Exactly one Organization, one Branch belonging to it, and the one
  Installation row naming that Branch are inserted here. Both names are
  copied from ``restaurant_profile.name`` (falling back to "Quick
  Junction", the value b1c4e7f9a2d5 seeds, if that row is missing or
  blank), so a fresh database and an existing one end up the same way.
- Each ``public_id`` is a random UUID generated here, once, locally -- no
  network is involved. ``hq_id`` stays NULL until a future HQ link.

No existing table, row, id or relationship is read for anything except the
restaurant name, and none is changed: no operational table gains a column,
users and roles are untouched, and no owner is chosen.

downgrade() drops only these three tables. The identity UUIDs go with them;
upgrading again creates a new identity, so restore a backup instead if the
original identity must be kept. Written by hand (reason in 2d9f3b20045f).
"""
import uuid

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4c7e2a9f1b38'
down_revision = 'e39e6dffcb5d'
branch_labels = None
depends_on = None

_FALLBACK_NAME = 'Quick Junction'
_NAME_MAX = 120


def _restaurant_name(conn) -> str:
    if 'restaurant_profile' not in sa.inspect(conn).get_table_names():
        return _FALLBACK_NAME
    name = conn.execute(sa.text("SELECT name FROM restaurant_profile WHERE id = 1")).scalar()
    name = (name or '').strip()
    return name[:_NAME_MAX] if name else _FALLBACK_NAME


def upgrade():
    op.create_table(
        'organizations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('hq_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
        sa.UniqueConstraint('hq_id'),
    )
    op.create_table(
        'branches',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('hq_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
        sa.UniqueConstraint('hq_id'),
    )
    op.create_table(
        'installation',
        # Not AUTO_INCREMENT: MySQL refuses a CHECK on one (3818), and the id is always 1.
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_installation_singleton'),
        sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )

    conn = op.get_bind()
    name = _restaurant_name(conn)
    organization_uuid = str(uuid.uuid4())
    branch_uuid = str(uuid.uuid4())
    installation_uuid = str(uuid.uuid4())

    conn.execute(sa.text("INSERT INTO organizations (public_id, name) VALUES (:p, :n)"),
                 {'p': organization_uuid, 'n': name})
    organization_id = conn.execute(sa.text("SELECT id FROM organizations WHERE public_id = :p"),
                                   {'p': organization_uuid}).scalar_one()
    conn.execute(sa.text("INSERT INTO branches (organization_id, public_id, name) VALUES (:o, :p, :n)"),
                 {'o': organization_id, 'p': branch_uuid, 'n': name})
    branch_id = conn.execute(sa.text("SELECT id FROM branches WHERE public_id = :p"),
                             {'p': branch_uuid}).scalar_one()
    conn.execute(sa.text("INSERT INTO installation (id, public_id, branch_id) VALUES (1, :p, :b)"),
                 {'p': installation_uuid, 'b': branch_id})


def downgrade():
    # Child first; nothing outside these three tables references them.
    op.drop_table('installation')
    op.drop_table('branches')
    op.drop_table('organizations')
