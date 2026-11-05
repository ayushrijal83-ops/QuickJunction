"""Ledger-backed payment recording and refunds (M7.5.2, wired into the live
staff routes by M7.5.3).

``payment_ledger_entries`` is the source of truth for every payment/refund
``app/routes/staff_orders.py`` records now -- see
``app/models/payment_ledger.py``. ``record_order_payment``/
``refund_order_payment`` call ``record_ledger_payment``/
``refund_ledger_payment`` directly; ``PaymentForm`` carries a real amount
field and ``RefundForm.reason`` is mandatory, and the refund route takes
the target payment entry's id in its URL (``/orders/<id>/payments/<entry_id>/
refund``) rather than assuming one payment per order. The legacy
``app/services/payments.py`` (``Payment``/``payments``, one row per order,
always for the order's full total) is no longer called by any route --
it remains only for pre-cutover historical data and the tests that still
exercise it directly. See the M7.5.3 report for the cutover's full scope,
including the still-open reconciliation gap for orders paid through the
legacy route between the M7.5.1 migration and this cutover actually
deploying (``app/templates/partials/_payment_badge.html`` and
``app/templates/staff/order_detail.html`` carry the UI-side mitigation).

Locking, same rule as ``app/services/payments.py`` and ``app/services/
orders.py``: every operation here locks the order row first (``SELECT ...
FOR UPDATE``) and holds it for the whole transaction, before it ever
touches a ``payment_ledger_entries`` row. That single lock is what makes
multiple payments/refunds against the same order safe without a second,
separate lock on the payment entry: every concurrent call against the same
order serializes behind it, so ``orders.amount_settled`` and a payment
entry's ``refunded_amount`` projection can both be moved with an ordinary
SQL-computed ``UPDATE ... SET x = x + :delta`` (never a Python-side
read-then-write) and still be race-free. The payment entry itself is only
ever touched by such a conditional ``UPDATE`` -- never its own
``SELECT ... FOR UPDATE`` -- so a payment lock is never acquired before the
order lock.

Audit (M7.5.2 correction): unlike every other service in this codebase
(``app/services/payments.py`` included), neither function here takes an
``audit`` callback. A caller-supplied callback can be omitted, swapped, or
made a no-op, which does not satisfy "payment/refund audit events are
unconditional" -- so each function writes its own fixed
``PAYMENT_RECORDED``/``PAYMENT_REFUNDED`` event directly, through
``record_event``, inside the same ``commit_with`` transaction as the
ledger write and the ``amount_settled``/``refunded_amount`` projection
update. There is nothing for a caller to bypass.

Cross-path note (resolved by M7.5.3, with one residual gap): ``app/
services/orders.py``'s cancellation guard (``holds_payment`` /
``_apply_transition``) and ``app/services/pricing.py``'s discount guard
now check ``orders.amount_settled`` exclusively, not the legacy
``payments`` table -- safe because this module's route cutover means
nothing live writes to ``payments`` for those guards to miss. The one
remaining gap is historical: an order paid through the legacy route
*after* the M7.5.1 migration ran but *before* this cutover actually
deployed has a ``payments`` row with no corresponding ledger entry, and
``amount_settled`` stuck at 0 for it. See the M7.5.3 report for the
required reconciliation procedure (not yet executed against any real
database) and ``_payment_badge.html``/``order_detail.html`` for the
interim UI guard against double-collecting such an order.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.order import Order, OrderStatus
from app.models.payment import PaymentMethod
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.models.user import User
from app.services.audit import commit_with, record_event
from app.services.errors import ValidationError

_CENTS = Decimal("0.01")


class LedgerPaymentError(ValidationError):
    pass


def _fail(message: str):
    raise LedgerPaymentError({"payment": [message]})


def _parse_amount(raw_amount, *, what: str) -> Decimal:
    try:
        amount = Decimal(str(raw_amount).strip()).quantize(_CENTS)
    except (InvalidOperation, ValueError):
        _fail(f"Enter a valid {what} amount.")
    if not amount.is_finite() or amount <= 0:
        _fail(f"The {what} amount must be greater than zero.")
    return amount


def _lock_order(order_id: int) -> Order:
    return db.session.execute(
        sa.select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def get_payment_entry_for_order(order_id: int, entry_id: int) -> PaymentLedgerEntry | None:
    """The 'payment' entry a refund route targets, scoped to its order so
    an entry id from a different order -- or a non-payment entry, such as
    a refund's own id -- can never be looked up as a refund target by
    guessing it. (``refund_ledger_payment`` re-checks ``entry_type`` itself
    regardless; this filter makes the contract the name promises true one
    layer earlier.)"""
    return db.session.query(PaymentLedgerEntry).filter_by(
        id=entry_id, order_id=order_id, entry_type=LedgerEntryType.PAYMENT
    ).first()


def record_ledger_payment(order: Order, raw_amount, raw_method, actor: User) -> PaymentLedgerEntry:
    """Record one 'payment' ledger entry against ``order``.

    Unlike ``app.services.payments.record_payment`` (exactly one payment,
    always the full total), this supports partial and multiple payments:
    ``raw_amount`` may be anything greater than zero and up to the order's
    current balance due (``total - amount_settled``).

    Writes its own fixed ``PAYMENT_RECORDED`` audit event every time the
    payment is accepted, in the same transaction -- there is no caller
    callback to omit, replace, or bypass it with.
    """
    try:
        method = PaymentMethod(raw_method)
    except ValueError:
        _fail("Unknown payment method.")

    amount = _parse_amount(raw_amount, what="payment")

    locked = _lock_order(order.id)
    if locked.status == OrderStatus.CANCELLED:
        db.session.rollback()
        _fail("A cancelled order cannot be paid.")

    # The balance-due check happens here, computed in SQL against the row
    # this call already holds locked -- not against a value read earlier in
    # Python -- so it is correct even if ``order`` is a stale in-memory copy.
    update = (
        sa.update(Order)
        .where(Order.id == locked.id, Order.amount_settled + amount <= Order.total)
        .values(amount_settled=Order.amount_settled + amount)
        .execution_options(synchronize_session=False)
    )
    entry = None
    audit_fn = None
    try:
        won = db.session.execute(update).rowcount == 1
        if won:
            entry = PaymentLedgerEntry(
                order_id=locked.id, entry_type=LedgerEntryType.PAYMENT, amount=amount, method=method,
                refunded_amount=Decimal("0.00"), corrected_amount=Decimal("0.00"), recorded_by_id=actor.id,
            )
            db.session.add(entry)

            def audit_fn():
                return record_event(
                    AuditEvent.PAYMENT_RECORDED, success=True, user_id=actor.id,
                    metadata={"order_id": locked.id, "payment_ledger_entry_id": entry.id,
                              "method": method.value, "amount": str(amount)},
                )
        commit_with(audit_fn)
    except SQLAlchemyError:
        db.session.rollback()
        _fail("Could not record the payment. Please try again.")
    db.session.refresh(order)
    if not won:
        _fail(f"Payment exceeds the balance due ({order.total - order.amount_settled}).")
    return entry


def refund_ledger_payment(payment_entry: PaymentLedgerEntry, raw_amount, reason: str | None,
                           actor: User) -> PaymentLedgerEntry:
    """Refund ``raw_amount`` (<= what remains unrefunded) against
    ``payment_entry``, which must be a 'payment' ledger row. Returns the new
    'refund' entry. A non-empty ``reason`` is mandatory (stricter than the
    legacy ``refund_payment``'s optional one).

    Writes its own fixed ``PAYMENT_REFUNDED`` audit event every time the
    refund is accepted, in the same transaction -- there is no caller
    callback to omit, replace, or bypass it with.
    """
    if payment_entry.entry_type != LedgerEntryType.PAYMENT:
        _fail("Only a payment entry can be refunded.")

    amount = _parse_amount(raw_amount, what="refund")
    reason = (reason or "").strip()[:255]
    if not reason:
        _fail("A refund reason is required.")

    locked = _lock_order(payment_entry.order_id)

    # Same shape as app.services.payments.refund_payment's conditional
    # UPDATE: the comparison runs against the current database row, so a
    # caller holding a stale in-memory refunded_amount cannot over-refund.
    update_payment = (
        sa.update(PaymentLedgerEntry)
        .where(PaymentLedgerEntry.id == payment_entry.id, PaymentLedgerEntry.entry_type == LedgerEntryType.PAYMENT,
               PaymentLedgerEntry.refunded_amount + amount <= PaymentLedgerEntry.amount)
        .values(refunded_amount=PaymentLedgerEntry.refunded_amount + amount)
        .execution_options(synchronize_session=False)
    )
    refund_entry = None
    audit_fn = None
    try:
        won = db.session.execute(update_payment).rowcount == 1
        if won:
            refund_entry = PaymentLedgerEntry(
                order_id=locked.id, entry_type=LedgerEntryType.REFUND, amount=-amount,
                reversed_entry_id=payment_entry.id, reason=reason, recorded_by_id=actor.id,
            )
            db.session.add(refund_entry)
            db.session.execute(
                sa.update(Order)
                .where(Order.id == locked.id)
                .values(amount_settled=Order.amount_settled - amount)
                .execution_options(synchronize_session=False)
            )

            def audit_fn():
                return record_event(
                    AuditEvent.PAYMENT_REFUNDED, success=True, user_id=actor.id,
                    metadata={"order_id": locked.id, "payment_ledger_entry_id": refund_entry.id,
                              "reversed_entry_id": payment_entry.id, "amount": str(amount), "reason": reason},
                )
        commit_with(audit_fn)
    except SQLAlchemyError:
        db.session.rollback()
        _fail("Could not record the refund. Please try again.")
    db.session.refresh(payment_entry)
    db.session.refresh(locked)
    if not won:
        _fail(f"Refund exceeds what remains refundable ({payment_entry.amount - payment_entry.refunded_amount}).")
    return refund_entry
