"""Tax and discounts (M12) -- the only place that prices them.

    discounted_subtotal = subtotal - discount_amount
    tax_amount          = discounted_subtotal x tax_rate / 100   (half-up to 0.01)
    total               = discounted_subtotal + tax_amount

Decimal throughout. An order's ``tax_rate`` is snapshotted at checkout and a
later discount re-prices with *that* rate, never the current setting, so no
past order ever changes when an admin edits the settings.

Who may discount (reusing the existing roles, no new one): customers never;
STAFF up to ``staff_max_discount`` % of the subtotal (a fixed amount is
measured as a percentage of the subtotal too); ADMIN up to 100 %. Every
non-zero discount needs a reason. A discount can only be applied while the
order is unpaid and not cancelled/completed -- the order row is locked
(``SELECT â€¦ FOR UPDATE``), the same lock ``record_payment`` takes, so a
payment can never be taken against a total that is about to change.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import sqlalchemy as sa

from app.extensions import db
from app.models.order import DiscountType, Order, OrderStatus
from app.models.payment import Payment
from app.models.pricing_settings import (
    DEFAULT_STAFF_MAX_DISCOUNT,
    DEFAULT_TAX_RATE,
    MAX_TAX_RATE,
    PricingSettings,
)
from app.models.user import Role, User
from app.services.errors import ValidationError

_CENTS = Decimal("0.01")
_HUNDRED = Decimal("100")
_SETTINGS_ID = 1
_DISCOUNTABLE = frozenset({OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING,
                           OrderStatus.READY, OrderStatus.SERVED})


class PricingError(ValidationError):
    pass


def _fail(field: str, message: str):
    raise PricingError({field: [message]})


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _decimal(raw, field: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        _fail(field, "Enter a number.")
    if not value.is_finite():
        _fail(field, "Enter a number.")
    return value


# --- settings ---------------------------------------------------------------------


def current_settings() -> tuple[Decimal, Decimal]:
    """(tax_rate %, staff_max_discount %)."""
    row = db.session.get(PricingSettings, _SETTINGS_ID)
    return (row.tax_rate, row.staff_max_discount) if row else (DEFAULT_TAX_RATE, DEFAULT_STAFF_MAX_DISCOUNT)


def update_settings(raw_tax_rate, raw_staff_max, actor: User) -> dict:
    """ADMIN only (enforced by the route). Returns {"from": ..., "to": ...}."""
    tax = _decimal(raw_tax_rate, "tax_rate").quantize(_CENTS)
    cap = _decimal(raw_staff_max, "staff_max_discount").quantize(_CENTS)
    if not Decimal("0") <= tax <= MAX_TAX_RATE:
        _fail("tax_rate", f"Tax rate must be between 0 and {MAX_TAX_RATE} %.")
    if not Decimal("0") <= cap <= _HUNDRED:
        _fail("staff_max_discount", "Staff maximum discount must be between 0 and 100 %.")
    before = current_settings()
    row = db.session.get(PricingSettings, _SETTINGS_ID) or PricingSettings(id=_SETTINGS_ID)
    row.tax_rate, row.staff_max_discount, row.updated_by_id = tax, cap, actor.id
    db.session.add(row)
    db.session.commit()
    return {"from": [str(v) for v in before], "to": [str(tax), str(cap)]}


# --- arithmetic -------------------------------------------------------------------


def price(subtotal: Decimal, discount_amount: Decimal, tax_rate: Decimal) -> tuple[Decimal, Decimal]:
    """(tax_amount, total) for an already-validated discount."""
    taxable = subtotal - discount_amount
    tax_amount = _money(taxable * tax_rate / _HUNDRED)
    return tax_amount, taxable + tax_amount


def discount_amount_for(discount_type: DiscountType, value: Decimal, subtotal: Decimal) -> Decimal:
    if discount_type == DiscountType.PERCENT:
        return _money(subtotal * value / _HUNDRED)
    return _money(value)


def preview(subtotal: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(tax_rate, tax_amount, total) a cart would be charged right now -- for
    display; checkout recomputes it server-side."""
    rate, _ = current_settings()
    tax_amount, total = price(subtotal, Decimal("0.00"), rate)
    return rate, tax_amount, total


# --- discounts --------------------------------------------------------------------


def apply_discount(order: Order, raw_type, raw_value, reason: str | None, actor: User) -> Order:
    """Set (or, with value 0, remove) the discount on an unpaid order and
    re-price it with the order's own snapshotted tax rate."""
    if actor.role not in (Role.STAFF, Role.ADMIN):
        _fail("discount", "Only staff can apply discounts.")
    try:
        discount_type = DiscountType(raw_type)
    except ValueError:
        _fail("discount_type", "Choose percentage or fixed amount.")
    value = _decimal(raw_value, "discount_value").quantize(_CENTS)
    reason = (reason or "").strip()
    if value < 0:
        _fail("discount_value", "A discount cannot be negative.")
    if value > 0 and not reason:
        _fail("discount_reason", "A reason is required for every discount.")
    if len(reason) > 255:
        _fail("discount_reason", "Keep the reason under 255 characters.")

    locked = db.session.execute(
        sa.select(Order).where(Order.id == order.id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    try:
        if locked.status not in _DISCOUNTABLE:
            _fail("discount", f"A {locked.status.value} order cannot be discounted.")
        # A *locking* read, not ``locked.payment``: under MySQL's REPEATABLE
        # READ a lazy load is a snapshot read and can miss a payment committed
        # after this transaction's first read -- found by the MySQL
        # discount-vs-payment race test. FOR UPDATE reads the latest commit.
        paid = db.session.execute(
            sa.select(Payment.id).where(Payment.order_id == locked.id).with_for_update()
        ).first()
        if paid is not None:
            _fail("discount", "This order is already paid; refund instead of discounting.")

        subtotal = locked.subtotal
        amount = discount_amount_for(discount_type, value, subtotal)
        if discount_type == DiscountType.PERCENT and value > _HUNDRED:
            _fail("discount_value", "A percentage discount cannot exceed 100 %.")
        if amount > subtotal:
            _fail("discount_value", "The discount cannot exceed the order subtotal.")
        cap = _HUNDRED if actor.role == Role.ADMIN else current_settings()[1]
        if subtotal > 0 and amount * _HUNDRED > subtotal * cap:
            _fail("discount_value", f"Your role may discount at most {cap} % of the subtotal.")
    except PricingError:
        db.session.rollback()  # release the row lock
        raise

    tax_amount, total = price(subtotal, amount, locked.tax_rate)
    if amount == 0:
        locked.discount_type = locked.discount_value = locked.discount_reason = locked.discounted_by_id = None
    else:
        locked.discount_type, locked.discount_value = discount_type, value
        locked.discount_reason, locked.discounted_by_id = reason, actor.id
    locked.discount_amount, locked.tax_amount, locked.total = amount, tax_amount, total
    db.session.commit()
    return locked
