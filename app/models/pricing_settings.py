"""The one authoritative place for the tax rate and the staff discount cap.

A single row (id = 1), seeded by migration d7f1e3a9c2b4 with the defaults
below; ``app/services/pricing.py`` reads it (falling back to these same
defaults if the row is missing) and is its only writer. Rates are percentages
stored as Numeric -- never float. Orders snapshot the rate they were charged
(``orders.tax_rate``), so changing it here never alters a past order.

13 % is this application's configured default, not a statement about any
jurisdiction's tax law; an admin sets the real value.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

DEFAULT_TAX_RATE = Decimal("13.00")
DEFAULT_STAFF_MAX_DISCOUNT = Decimal("20.00")
MAX_TAX_RATE = Decimal("50.00")  # sanity bound; no sensible sales tax is higher


class PricingSettings(db.Model):
    __tablename__ = "pricing_settings"
    __table_args__ = (
        sa.CheckConstraint(f"tax_rate >= 0 AND tax_rate <= {MAX_TAX_RATE}", name="ck_pricing_settings_tax_rate_range"),
        sa.CheckConstraint(
            "staff_max_discount >= 0 AND staff_max_discount <= 100", name="ck_pricing_settings_staff_max_range"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tax_rate: Mapped[Decimal] = mapped_column(sa.Numeric(5, 2), nullable=False)             # percent
    staff_max_discount: Mapped[Decimal] = mapped_column(sa.Numeric(5, 2), nullable=False)   # percent of subtotal
    updated_by_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )
