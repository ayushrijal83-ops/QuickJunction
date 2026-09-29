"""Ingredient -- a named menu attribute (recommendations, M03) and, since M13,
an inventory item.

Stock fields: ``current_quantity`` is only ever changed together with a
``StockMovement`` row (app/services/inventory.py), so every change is
traceable. ``minimum_quantity`` drives the low-stock flag. ``is_active``
means "tracked in inventory": an inactive ingredient is still a menu
attribute but is neither checked at checkout nor deducted on sale.

Stock may go below zero -- deliberately no CHECK: a dish already served is
never blocked from completing because of a count mismatch; the negative
figure is shown as "short" so staff fix the count instead.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import enum_column

QTY = sa.Numeric(12, 3)


class StockUnit(str, enum.Enum):
    GRAM = "g"
    KILOGRAM = "kg"
    MILLILITRE = "ml"
    LITRE = "l"
    PIECE = "piece"


class Ingredient(db.Model):
    __tablename__ = "ingredients"
    __table_args__ = (
        sa.CheckConstraint("minimum_quantity >= 0", name="ck_ingredients_minimum_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80), unique=True, nullable=False)
    unit: Mapped[StockUnit] = mapped_column(
        enum_column(StockUnit, 16, name="ck_ingredients_unit"), nullable=False, server_default=StockUnit.PIECE.value
    )
    current_quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False, server_default="0", default=Decimal("0"))
    minimum_quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False, server_default="0", default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true(), default=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    @property
    def is_low(self) -> bool:
        return self.is_active and self.current_quantity <= self.minimum_quantity and (
            self.minimum_quantity > 0 or self.current_quantity < 0)

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Ingredient id={self.id} name={self.name!r} qty={self.current_quantity}>"
