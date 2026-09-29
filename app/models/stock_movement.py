"""Stock ledger (M13): one row per change to an ingredient's quantity.

``quantity_change`` is signed (+ in, - out); ``quantity_after`` is the
ingredient's level right after the change, so the history reads like a bank
statement. SALE rows carry the order that consumed the stock, and
``uq_stock_movements_order_ingredient`` makes a sale deduction idempotent at
the database level: one order can deduct one ingredient once, however many
times completion is retried. Manual rows have ``order_id`` NULL, which never
collides (NULLs are distinct in a unique index on MySQL and SQLite).
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import enum_column
from app.models.ingredient import QTY


class MovementType(str, enum.Enum):
    PURCHASE = "purchase"      # new stock bought in
    RESTOCK = "restock"        # stock brought back into use (e.g. from storage)
    SALE = "sale"              # consumed by a completed order (automatic)
    WASTE = "waste"            # spoiled, dropped, expired
    ADJUSTMENT = "adjustment"  # stock-count correction, either direction (admin)


class StockMovement(db.Model):
    __tablename__ = "stock_movements"
    __table_args__ = (
        sa.UniqueConstraint("order_id", "ingredient_id", name="uq_stock_movements_order_ingredient"),
        sa.CheckConstraint("quantity_change <> 0", name="ck_stock_movements_change_nonzero"),
        sa.CheckConstraint(
            "(movement_type = 'sale' AND order_id IS NOT NULL) OR (movement_type <> 'sale' AND order_id IS NULL)",
            name="ck_stock_movements_sale_has_order",
        ),
        sa.Index("ix_stock_movements_ingredient_created", "ingredient_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False
    )
    movement_type: Mapped[MovementType] = mapped_column(
        enum_column(MovementType, 16, name="ck_stock_movements_movement_type"), nullable=False
    )
    quantity_change: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    quantity_after: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    order_id: Mapped[int | None] = mapped_column(sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    note: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)

    ingredient: Mapped["Ingredient"] = relationship()  # noqa: F821
    actor: Mapped["User | None"] = relationship()  # noqa: F821
