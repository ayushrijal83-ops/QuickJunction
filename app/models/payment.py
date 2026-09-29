"""Payments -- the record that money was actually received for an order.

One payment per order (``order_id`` UNIQUE), taken at the counter by staff;
there is no card gateway. ``amount`` is always the order's server-side
``total`` at the moment of payment, never a client value. Refunds reduce
``refunded_amount`` towards ``amount`` and never beyond it -- enforced by a
CHECK as well as by the service's conditional UPDATE.

Money is Numeric end to end (never float), same rule as ``Order``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import enum_column


class PaymentMethod(str, enum.Enum):
    CASH = "cash"
    CARD = "card"      # card terminal at the counter; no card data is stored
    WALLET = "wallet"  # mobile wallet / QR


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint(
            "refunded_amount >= 0 AND refunded_amount <= amount", name="ck_payments_refund_within_amount"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # UNIQUE: an order can be paid once -- a double submit or two cashiers at
    # once cannot both commit.
    order_id: Mapped[int] = mapped_column(
        sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    method: Mapped[PaymentMethod] = mapped_column(
        enum_column(PaymentMethod, 16, name="ck_payments_method"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)
    refunded_amount: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 2), nullable=False, server_default="0", default=Decimal("0.00")
    )

    captured_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    recorded_by_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    # Most recent refund; every refund is also an audit event with its amount.
    refunded_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    refund_reason: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    order: Mapped["Order"] = relationship(back_populates="payment")  # noqa: F821

    @property
    def net_amount(self) -> Decimal:
        return self.amount - self.refunded_amount

    @property
    def fully_refunded(self) -> bool:
        return self.refunded_amount >= self.amount

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Payment id={self.id} order_id={self.order_id} amount={self.amount} refunded={self.refunded_amount}>"
