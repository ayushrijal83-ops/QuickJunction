"""Payment ledger (M7.5.1): the append-only financial record for orders.

M7.5.1 added the schema and backfilled history from the existing
``payments`` table (see the ``e39e6dffcb5d`` migration). M7.5.2
(``app/services/payment_ledger.py``) writes new entries here going
forward for any order paid/refunded through that service -- but
``app/services/payments.py`` (``Payment``/``payments``) is still what
``app/routes/staff_orders.py`` calls, unchanged: routing it to the ledger
needs a UI change (an amount field, a payment selector for refunds) out of
scope for M7.5.2. Until that routing lands, ``Payment``/``payments`` stays
the record new staff-recorded payments actually land in.

One row per financial event against an order:

- ``payment``    -- amount received. Carries the running projections
  ``refunded_amount`` and ``corrected_amount`` for that payment (NULL on
  every other entry_type -- see ``ck_payment_ledger_entries_projection_fields``).
- ``refund``      -- money returned; ``reversed_entry_id`` names the payment
  entry it reduces.
- ``adjustment``  -- a standalone correction not tied to a specific payment
  (no ``reversed_entry_id``).
- ``correction``  -- a correction to a specific payment; like ``refund``,
  ``reversed_entry_id`` names it.

``ck_payment_ledger_entries_effective_nonnegative`` (``amount -
refunded_amount + corrected_amount >= 0`` on a payment row) is, rearranged,
exactly "refunds [net of corrections] cannot exceed the effective payment
amount" -- the same business rule stated two ways, not two separate checks.

The FK on ``reversed_entry_id`` only guarantees the referenced row exists --
not that it is itself a ``payment`` entry. That check (and "refunds cannot
exceed the effective payment amount" as a *write-time*, not just
*at-rest*, guarantee under concurrency) belongs to the future service layer,
the same way ``Payment``'s per-request invariants are enforced in
``app/services/payments.py`` on top of its own CHECK constraints.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import enum_column
from app.models.payment import PaymentMethod


class LedgerEntryType(str, enum.Enum):
    PAYMENT = "payment"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    CORRECTION = "correction"


class PaymentLedgerEntry(db.Model):
    __tablename__ = "payment_ledger_entries"
    __table_args__ = (
        sa.CheckConstraint(
            "(entry_type = 'payment' AND amount > 0) OR "
            "(entry_type = 'refund' AND amount < 0) OR "
            "(entry_type IN ('adjustment', 'correction') AND amount <> 0)",
            name="ck_payment_ledger_entries_amount_sign",
        ),
        sa.CheckConstraint(
            "(entry_type = 'payment' AND method IS NOT NULL) OR "
            "(entry_type <> 'payment' AND method IS NULL)",
            name="ck_payment_ledger_entries_method_presence",
        ),
        sa.CheckConstraint(
            "(entry_type IN ('refund', 'correction') AND reversed_entry_id IS NOT NULL) OR "
            "(entry_type NOT IN ('refund', 'correction') AND reversed_entry_id IS NULL)",
            name="ck_payment_ledger_entries_reversed_entry_presence",
        ),
        # Projections live only on the payment row they summarise; every
        # other entry_type must carry NULL in both. refunded_amount is a
        # magnitude (>= 0) independent of any correction.
        sa.CheckConstraint(
            "(entry_type = 'payment' AND refunded_amount IS NOT NULL AND refunded_amount >= 0 "
            "AND corrected_amount IS NOT NULL) OR "
            "(entry_type <> 'payment' AND refunded_amount IS NULL AND corrected_amount IS NULL)",
            name="ck_payment_ledger_entries_projection_fields",
        ),
        # See the module docstring: this is also "refunds cannot exceed the
        # effective payment amount", rearranged.
        sa.CheckConstraint(
            "entry_type <> 'payment' OR (amount - refunded_amount + corrected_amount) >= 0",
            name="ck_payment_ledger_entries_effective_nonnegative",
        ),
        # recorded_by_id is required on every entry_type except 'refund'
        # (a historical backfilled refund cannot reliably name an actor --
        # see recorded_by_id below). This cannot also forbid NULL on a
        # *newly created* refund without a provenance column the approved
        # schema doesn't have: that half of the rule is a service-layer
        # responsibility once a later milestone starts writing refunds here.
        sa.CheckConstraint(
            "entry_type = 'refund' OR recorded_by_id IS NOT NULL",
            name="ck_payment_ledger_entries_recorded_by_presence",
        ),
        sa.Index("ix_payment_ledger_entries_order_created", "order_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # RESTRICT: a financial record must never be silently orphaned (same
    # rule as Order.user_id / Payment.order_id).
    order_id: Mapped[int] = mapped_column(
        sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    entry_type: Mapped[LedgerEntryType] = mapped_column(
        enum_column(LedgerEntryType, 16, name="ck_payment_ledger_entries_entry_type"), nullable=False
    )

    # Signed: positive for payment, negative for refund, either sign for
    # adjustment/correction (ck_payment_ledger_entries_amount_sign).
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)

    # Required for 'payment', NULL for every other entry_type
    # (ck_payment_ledger_entries_method_presence).
    method: Mapped[PaymentMethod | None] = mapped_column(
        enum_column(PaymentMethod, 16, name="ck_payment_ledger_entries_method"), nullable=True
    )

    # Running projections carried only on the 'payment' row they summarise
    # (ck_payment_ledger_entries_projection_fields) -- NULL on every other
    # entry_type. Mirrors Payment.refunded_amount's role for the legacy
    # table; corrected_amount is new here (signed: a correction can move the
    # effective amount either direction).
    refunded_amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2), nullable=True)
    corrected_amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2), nullable=True)

    # Required for 'refund'/'correction' (what this entry reverses or
    # corrects), NULL otherwise (ck_payment_ledger_entries_reversed_entry_presence).
    reversed_entry_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("payment_ledger_entries.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    reason: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    # Nullable only for 'refund' (ck_payment_ledger_entries_recorded_by_presence):
    # a historical refund backfilled from the legacy schema cannot reliably
    # name who issued it (see the M7.5.1 migration). Every entry this
    # milestone's own backfill writes for 'payment' carries a value, copied
    # from payments.recorded_by_id (NOT NULL there).
    recorded_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="ledger_entries")  # noqa: F821
    recorded_by: Mapped["User | None"] = relationship()  # noqa: F821
    reversed_entry: Mapped["PaymentLedgerEntry | None"] = relationship(remote_side=[id])

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (f"<PaymentLedgerEntry id={self.id} order_id={self.order_id} "
                f"type={self.entry_type.value} amount={self.amount}>")
