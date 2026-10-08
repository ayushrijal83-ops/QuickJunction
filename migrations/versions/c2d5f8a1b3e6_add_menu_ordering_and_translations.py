"""add menu ordering and translations

Revision ID: c2d5f8a1b3e6
Revises: b1c4e7f9a2d5
Create Date: 2026-10-08 18:10:00.000000

M5 (menu & restaurant foundation), part 2 of 2.

- ``categories.sort_order`` and ``menu_items.sort_order``: INTEGER NOT NULL
  DEFAULT 0, CHECK 0..10000. Every existing row gets 0, so the menu keeps its
  current (alphabetical) order until an admin sets one.
- ``category_translations`` / ``menu_item_translations``: per-locale name
  and description for the customer menu, UNIQUE (entity, locale), CHECK
  locale IN ('en', 'ne'), ON DELETE CASCADE. Created empty: the existing
  name/description columns stay the canonical text and nothing is copied
  or machine-translated.

Downgrade drops the tables (and any translations in them) and the columns.
Written by hand (reason in 2d9f3b20045f).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2d5f8a1b3e6'
down_revision = 'b1c4e7f9a2d5'
branch_labels = None
depends_on = None

_SORT_RANGE = 'sort_order >= 0 AND sort_order <= 10000'
_LOCALES = "locale IN ('en', 'ne')"


def _translation_table(table: str, fk_column: str, parent: str, unique_name: str, check_name: str,
                       name_length: int, description_length: int) -> None:
    op.create_table(
        table,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(fk_column, sa.Integer(), nullable=False),
        sa.Column('locale', sa.String(length=8), nullable=False),
        sa.Column('name', sa.String(length=name_length), nullable=False),
        sa.Column('description', sa.String(length=description_length), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint(_LOCALES, name=check_name),
        sa.ForeignKeyConstraint([fk_column], [f'{parent}.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(fk_column, 'locale', name=unique_name),
    )


def _sqlite_foreign_keys(enabled: bool) -> None:
    """SQLite batch mode rebuilds ``categories``/``menu_items`` (drop + copy),
    which fails while rows elsewhere reference them and foreign keys are
    enforced. MySQL alters in place and needs nothing."""
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        bind.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade():
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False))
        batch_op.create_check_constraint('ck_categories_sort_order_range', _SORT_RANGE)
    with op.batch_alter_table('menu_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False))
        batch_op.create_check_constraint('ck_menu_items_sort_order_range', _SORT_RANGE)
    _sqlite_foreign_keys(True)

    _translation_table('category_translations', 'category_id', 'categories',
                       'uq_category_translations_category_locale', 'ck_category_translations_locale', 80, 500)
    _translation_table('menu_item_translations', 'menu_item_id', 'menu_items',
                       'uq_menu_item_translations_item_locale', 'ck_menu_item_translations_locale', 120, 2000)


def downgrade():
    # drop_table alone: dropping the FK-backed unique index first fails on
    # MySQL (1553; known issue #32).
    op.drop_table('menu_item_translations')
    op.drop_table('category_translations')
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('menu_items', schema=None) as batch_op:
        batch_op.drop_constraint('ck_menu_items_sort_order_range', type_='check')
        batch_op.drop_column('sort_order')
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.drop_constraint('ck_categories_sort_order_range', type_='check')
        batch_op.drop_column('sort_order')
    _sqlite_foreign_keys(True)
