"""Restaurant (dining) tables.

Named ``RestaurantTable`` / ``restaurant_tables`` so it never collides with
SQLAlchemy's own ``Table``. A table's *status* is operational state that staff
move through ``app/services/tables.py``'s transition allow-list; it is never
used to rewrite history -- an order keeps its ``table_id`` forever, whatever
the table does afterwards.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import enum_column

MIN_CAPACITY = 1
MAX_CAPACITY = 50


class TableStatus(str, enum.Enum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    RESERVED = "reserved"
    CLEANING = "cleaning"
    OUT_OF_SERVICE = "out_of_service"


class RestaurantTable(db.Model):
    __tablename__ = "restaurant_tables"
    __table_args__ = (
        sa.CheckConstraint(
            f"capacity >= {MIN_CAPACITY} AND capacity <= {MAX_CAPACITY}",
            name="ck_restaurant_tables_capacity_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Display name staff use ("T05", "Patio 2"). Unique so a dine-in order's
    # table is never ambiguous on a ticket or in a report.
    name: Mapped[str] = mapped_column(sa.String(40), unique=True, nullable=False)
    capacity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[TableStatus] = mapped_column(
        enum_column(TableStatus, 16, name="ck_restaurant_tables_status"),
        nullable=False,
        server_default=TableStatus.AVAILABLE.value,
    )

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<RestaurantTable id={self.id} name={self.name!r} status={self.status.value}>"
