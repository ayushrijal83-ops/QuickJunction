"""add payment ledger

Revision ID: e39e6dffcb5d
Revises: c2d5f8a1b3e6
Create Date: 2026-11-04 21:00:00.000000

Milestone 7.5.1 (payment ledger foundation).

``payment_ledger_entries`` is the new append-only financial ledger: one row
per payment, refund, adjustment or correction. This revision only creates
the schema and backfills history from the existing ``payments`` table -- no
application code writes to it yet (a later milestone wires the payment/
refund services to it). ``payments`` / ``Payment`` stay authoritative and
are neither touched nor removed here.

Backfill, for every existing ``payments`` row:
  - one 'payment' ledger entry, preserving order_id, amount, method,
    recorded_by_id and captured_at (-> created_at). refunded_amount is
    carried over as the final historical total (not replayed event by
    event); corrected_amount starts at 0 -- nothing in the legacy schema
    ever recorded a correction.
  - if refunded_amount > 0, one 'refund' ledger entry (amount =
    -refunded_amount, reversed_entry_id = the payment entry above, reason =
    refund_reason, created_at = refunded_at, falling back to captured_at in
    the data-theoretic case of a positive refunded_amount with no
    refunded_at). recorded_by_id is NULL: the legacy schema does not
    reliably preserve who issued a refund -- the only entry_type this
    schema permits that for (ck_payment_ledger_entries_recorded_by_presence);
    payment/adjustment/correction always require it.
  - orders.amount_settled is set to amount - refunded_amount for every
    order with a payment; it keeps its default (0) for orders with none.
    Nothing is invented for orders that were never paid.

The backfill is guarded by NOT EXISTS, so it is safe to run again (e.g.
after a retry) without duplicating ledger rows.

Downgrade safety is mandatory and, once anything beyond this migration's own
historical backfill exists, destructive-refusing by design:

  _preflight_downgrade (read-only) refuses if:
    - an order has more than one 'payment' ledger entry,
    - any 'adjustment' or 'correction' entry exists,
    - a payment has more than one linked 'refund' entry,
    - a payment's refunded_amount projection does not match the sum of its
      linked refund entries,
    - a legacy payments row has no corresponding ledger payment entry.
  None of these can arise from this migration's own backfill or from any
  route today (nothing else writes to this table yet); they only become
  possible once a later milestone starts writing here.

  _reconstruct_payments_from_ledger then upserts ``payments`` from the
  ledger's current state (keyed on payments.order_id, already UNIQUE), so
  the legacy table is rebuilt from the ledger rather than assumed to still
  be correct. _verify_reconstruction then checks every ledger payment entry
  maps to exactly one payments row before anything is dropped.

  **MySQL DDL implicitly commits.** The preflight, reconstruction and
  verification above run as ordinary DML before any DDL statement in this
  function, so on both SQLite and MySQL they succeed or fail together inside
  the migration's transaction with nothing yet written to disk at the schema
  level. The DDL that follows (dropping the amount_settled column and
  dropping payment_ledger_entries) is **not** part of that same atomic unit
  on MySQL: a failure partway through the DDL (e.g. the process killed
  between the two drops) can leave the schema downgraded further than the
  data, even though every row of financial history itself is intact. Run
  downgrade only in a maintenance window with every application financial
  write (recording a payment, issuing a refund) stopped for its entire
  duration, from before preflight starts until after the DDL completes.

Written by hand, following the conventions in 2d9f3b20045f (hand-written
migrations) and c3a9d7e21f58 / e5b8c1d4f7a2 (conditional-presence CHECK
constraints, FK-backed ledger tables).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e39e6dffcb5d'
down_revision = 'c2d5f8a1b3e6'
branch_labels = None
depends_on = None


class LedgerDowngradeRefused(RuntimeError):
    """Raised by _preflight_downgrade or _verify_reconstruction when the
    ledger holds (or, after reconstruction, would lose) something the legacy
    payments schema cannot represent. Every check that raises this is
    read-only up to the point it raises -- see the module docstring."""


_BACKFILL_PAYMENT_ENTRIES_SQL = """
    INSERT INTO payment_ledger_entries
        (order_id, entry_type, amount, method, refunded_amount, corrected_amount,
         reversed_entry_id, reason, recorded_by_id, created_at)
    SELECT p.order_id, 'payment', p.amount, p.method, p.refunded_amount, 0,
           NULL, NULL, p.recorded_by_id, p.captured_at
    FROM payments p
    WHERE NOT EXISTS (
        SELECT 1 FROM payment_ledger_entries l
        WHERE l.order_id = p.order_id AND l.entry_type = 'payment'
    )
"""

_BACKFILL_REFUND_ENTRIES_SQL = """
    INSERT INTO payment_ledger_entries
        (order_id, entry_type, amount, method, refunded_amount, corrected_amount,
         reversed_entry_id, reason, recorded_by_id, created_at)
    SELECT p.order_id, 'refund', -p.refunded_amount, NULL, NULL, NULL,
           l.id, p.refund_reason, NULL, COALESCE(p.refunded_at, p.captured_at)
    FROM payments p
    JOIN payment_ledger_entries l ON l.order_id = p.order_id AND l.entry_type = 'payment'
    WHERE p.refunded_amount > 0
      AND NOT EXISTS (
          SELECT 1 FROM payment_ledger_entries r
          WHERE r.entry_type = 'refund' AND r.reversed_entry_id = l.id
      )
"""

_BACKFILL_SETTLEMENT_SQL = """
    UPDATE orders
    SET amount_settled = (
        SELECT p.amount - p.refunded_amount FROM payments p WHERE p.order_id = orders.id
    )
    WHERE EXISTS (SELECT 1 FROM payments p WHERE p.order_id = orders.id)
"""

_RECONSTRUCT_PAYMENTS_SQLITE_SQL = """
    INSERT INTO payments
        (order_id, method, amount, refunded_amount, captured_at, recorded_by_id, refunded_at, refund_reason, updated_at)
    SELECT l.order_id, l.method, l.amount, COALESCE(l.refunded_amount, 0), l.created_at, l.recorded_by_id,
           r.created_at, r.reason, CURRENT_TIMESTAMP
    FROM payment_ledger_entries l
    LEFT JOIN payment_ledger_entries r ON r.entry_type = 'refund' AND r.reversed_entry_id = l.id
    WHERE l.entry_type = 'payment'
    ON CONFLICT(order_id) DO UPDATE SET
        method = excluded.method,
        amount = excluded.amount,
        refunded_amount = excluded.refunded_amount,
        captured_at = excluded.captured_at,
        recorded_by_id = excluded.recorded_by_id,
        refunded_at = excluded.refunded_at,
        refund_reason = excluded.refund_reason,
        updated_at = excluded.updated_at
"""

_RECONSTRUCT_PAYMENTS_MYSQL_SQL = """
    INSERT INTO payments
        (order_id, method, amount, refunded_amount, captured_at, recorded_by_id, refunded_at, refund_reason, updated_at)
    SELECT l.order_id, l.method, l.amount, COALESCE(l.refunded_amount, 0), l.created_at, l.recorded_by_id,
           r.created_at, r.reason, NOW()
    FROM payment_ledger_entries l
    LEFT JOIN payment_ledger_entries r ON r.entry_type = 'refund' AND r.reversed_entry_id = l.id
    WHERE l.entry_type = 'payment'
    ON DUPLICATE KEY UPDATE
        method = VALUES(method),
        amount = VALUES(amount),
        refunded_amount = VALUES(refunded_amount),
        captured_at = VALUES(captured_at),
        recorded_by_id = VALUES(recorded_by_id),
        refunded_at = VALUES(refunded_at),
        refund_reason = VALUES(refund_reason),
        updated_at = VALUES(updated_at)
"""


def upgrade():
    op.create_table(
        'payment_ledger_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('entry_type', sa.String(length=16), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('method', sa.String(length=16), nullable=True),
        sa.Column('refunded_amount', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('corrected_amount', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('reversed_entry_id', sa.Integer(), nullable=True),
        sa.Column('reason', sa.String(length=255), nullable=True),
        sa.Column('recorded_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint(
            "entry_type IN ('payment', 'refund', 'adjustment', 'correction')",
            name='ck_payment_ledger_entries_entry_type',
        ),
        sa.CheckConstraint(
            "(entry_type = 'payment' AND amount > 0) OR "
            "(entry_type = 'refund' AND amount < 0) OR "
            "(entry_type IN ('adjustment', 'correction') AND amount <> 0)",
            name='ck_payment_ledger_entries_amount_sign',
        ),
        sa.CheckConstraint(
            "method IN ('cash', 'card', 'wallet')",
            name='ck_payment_ledger_entries_method',
        ),
        sa.CheckConstraint(
            "(entry_type = 'payment' AND method IS NOT NULL) OR "
            "(entry_type <> 'payment' AND method IS NULL)",
            name='ck_payment_ledger_entries_method_presence',
        ),
        sa.CheckConstraint(
            "(entry_type IN ('refund', 'correction') AND reversed_entry_id IS NOT NULL) OR "
            "(entry_type NOT IN ('refund', 'correction') AND reversed_entry_id IS NULL)",
            name='ck_payment_ledger_entries_reversed_entry_presence',
        ),
        sa.CheckConstraint(
            "(entry_type = 'payment' AND refunded_amount IS NOT NULL AND refunded_amount >= 0 "
            "AND corrected_amount IS NOT NULL) OR "
            "(entry_type <> 'payment' AND refunded_amount IS NULL AND corrected_amount IS NULL)",
            name='ck_payment_ledger_entries_projection_fields',
        ),
        sa.CheckConstraint(
            "entry_type <> 'payment' OR (amount - refunded_amount + corrected_amount) >= 0",
            name='ck_payment_ledger_entries_effective_nonnegative',
        ),
        sa.CheckConstraint(
            "entry_type = 'refund' OR recorded_by_id IS NOT NULL",
            name='ck_payment_ledger_entries_recorded_by_presence',
        ),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['recorded_by_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['reversed_entry_id'], ['payment_ledger_entries.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('payment_ledger_entries', schema=None) as batch_op:
        batch_op.create_index('ix_payment_ledger_entries_order_id', ['order_id'], unique=False)
        batch_op.create_index('ix_payment_ledger_entries_reversed_entry_id', ['reversed_entry_id'], unique=False)
        batch_op.create_index('ix_payment_ledger_entries_order_created', ['order_id', 'created_at'], unique=False)

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('amount_settled', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False)
        )
        batch_op.create_check_constraint('ck_orders_amount_settled_nonnegative', 'amount_settled >= 0')
        batch_op.create_check_constraint('ck_orders_amount_settled_within_total', 'amount_settled <= total')

    op.execute(_BACKFILL_PAYMENT_ENTRIES_SQL)
    op.execute(_BACKFILL_REFUND_ENTRIES_SQL)
    op.execute(_BACKFILL_SETTLEMENT_SQL)


def _preflight_downgrade(bind):
    multi_payment = bind.execute(sa.text(
        "SELECT order_id, COUNT(*) AS c FROM payment_ledger_entries WHERE entry_type = 'payment' "
        "GROUP BY order_id HAVING COUNT(*) > 1"
    )).fetchall()
    if multi_payment:
        raise LedgerDowngradeRefused(
            f"{len(multi_payment)} order(s) have more than one payment ledger entry; the legacy "
            "payments table allows exactly one per order."
        )

    non_backfillable = bind.execute(sa.text(
        "SELECT COUNT(*) FROM payment_ledger_entries WHERE entry_type IN ('adjustment', 'correction')"
    )).scalar()
    if non_backfillable:
        raise LedgerDowngradeRefused(
            f"{non_backfillable} adjustment/correction ledger entries exist; the legacy payments "
            "table has no column to represent them."
        )

    multi_refund = bind.execute(sa.text(
        "SELECT reversed_entry_id, COUNT(*) AS c FROM payment_ledger_entries WHERE entry_type = 'refund' "
        "GROUP BY reversed_entry_id HAVING COUNT(*) > 1"
    )).fetchall()
    if multi_refund:
        raise LedgerDowngradeRefused(
            f"{len(multi_refund)} payment(s) have more than one linked refund entry; the legacy "
            "payments table has only one refunded_amount per payment."
        )

    mismatched = bind.execute(sa.text("""
        SELECT l.id FROM payment_ledger_entries l
        WHERE l.entry_type = 'payment'
          AND COALESCE(l.refunded_amount, 0) <> COALESCE((
              SELECT SUM(-r.amount) FROM payment_ledger_entries r
              WHERE r.entry_type = 'refund' AND r.reversed_entry_id = l.id
          ), 0)
    """)).fetchall()
    if mismatched:
        raise LedgerDowngradeRefused(
            f"{len(mismatched)} payment entry/entries have a refunded_amount that does not match "
            "the sum of their linked refund entries."
        )

    orphan_legacy = bind.execute(sa.text("""
        SELECT p.id FROM payments p
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_ledger_entries l
            WHERE l.entry_type = 'payment' AND l.order_id = p.order_id
        )
    """)).fetchall()
    if orphan_legacy:
        raise LedgerDowngradeRefused(
            f"{len(orphan_legacy)} legacy payments row(s) have no corresponding ledger payment "
            "entry; reconstruction would lose them."
        )


def _reconstruct_payments_from_ledger(bind):
    """Transactional DML only (see module docstring). Preflight above has
    already guaranteed at most one refund per payment entry, so the LEFT
    JOIN below matches at most one 'refund' row."""
    sql = _RECONSTRUCT_PAYMENTS_SQLITE_SQL if bind.dialect.name == 'sqlite' else _RECONSTRUCT_PAYMENTS_MYSQL_SQL
    bind.execute(sa.text(sql))


def _verify_reconstruction(bind):
    mismatch = bind.execute(sa.text("""
        SELECT l.id FROM payment_ledger_entries l
        WHERE l.entry_type = 'payment'
          AND (SELECT COUNT(*) FROM payments p WHERE p.order_id = l.order_id) <> 1
    """)).fetchall()
    if mismatch:
        raise LedgerDowngradeRefused(
            f"{len(mismatch)} ledger payment entries do not map to exactly one legacy payments row "
            "after reconstruction; refusing to drop the ledger table."
        )


def downgrade():
    bind = op.get_bind()

    # Read-only, then transactional DML -- both ahead of any DDL below, so a
    # refusal or a reconstruction failure leaves nothing written. See the
    # module docstring for why the DDL that follows is not part of this same
    # atomic unit on MySQL.
    _preflight_downgrade(bind)
    _reconstruct_payments_from_ledger(bind)
    _verify_reconstruction(bind)

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_constraint('ck_orders_amount_settled_within_total', type_='check')
        batch_op.drop_constraint('ck_orders_amount_settled_nonnegative', type_='check')
        batch_op.drop_column('amount_settled')

    op.drop_table('payment_ledger_entries')
