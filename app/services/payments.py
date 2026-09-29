"""Recording payments and refunds.

Race safety, by the same rules as order transitions:

- **Double payment** -- ``payments.order_id`` is UNIQUE; the second insert
  fails and becomes "already paid".
- **Payment vs cancellation** -- ``record_payment`` locks the order row
  (``SELECT … FOR UPDATE``) and re-reads its status before inserting, and the
  cancel UPDATE (``orders._apply_transition``) refuses while an unrefunded
  payment exists. Both contend for the same order row, so a payment can never
  land on an order that is being cancelled, nor a cancel succeed on a paid one.
- **Over-refund** -- one conditional ``UPDATE … WHERE refunded_amount + x <=
  amount``; two concurrent refunds cannot together exceed the payment. Backed
  by ``ck_payments_refund_within_amount``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentMethod
from app.models.user import User
from app.services.errors import ValidationError

_CENTS = Decimal("0.01")


class PaymentError(ValidationError):
    pass


def _fail(message: str):
    raise PaymentError({"payment": [message]})


def record_payment(order: Order, raw_method, actor: User) -> Payment:
    try:
        method = PaymentMethod(raw_method)
    except ValueError:
        _fail("Unknown payment method.")

    # Lock the row and re-read it: the status that matters is the one at the
    # moment of payment, not the one the page was rendered with.
    locked = db.session.execute(
        sa.select(Order).where(Order.id == order.id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    if locked.status == OrderStatus.CANCELLED:
        db.session.rollback()
        _fail("A cancelled order cannot be paid.")
    if locked.payment is not None:
        db.session.rollback()
        _fail(f"Order #{order.id} is already paid.")

    payment = Payment(order_id=locked.id, method=method, amount=locked.total, recorded_by_id=actor.id)
    db.session.add(payment)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _fail(f"Order #{order.id} is already paid.")
    return payment


def refund_payment(payment: Payment, raw_amount, reason: str | None) -> Decimal:
    """Refund ``raw_amount`` (≤ what is still unrefunded). Returns the amount."""
    try:
        amount = Decimal(str(raw_amount).strip()).quantize(_CENTS)
    except (InvalidOperation, ValueError):
        _fail("Enter a valid refund amount.")
    if not amount.is_finite() or amount <= 0:
        _fail("The refund amount must be greater than zero.")

    stmt = (
        sa.update(Payment)
        .where(Payment.id == payment.id, Payment.refunded_amount + amount <= Payment.amount)
        .values(refunded_amount=Payment.refunded_amount + amount, refunded_at=sa.func.now(),
                refund_reason=(reason or "").strip()[:255] or None)
        .execution_options(synchronize_session=False)
    )
    try:
        won = db.session.execute(stmt).rowcount == 1
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        _fail("Could not record the refund. Please try again.")
    db.session.refresh(payment)
    if not won:
        _fail(f"Refund exceeds what remains refundable ({payment.net_amount}).")
    return amount
