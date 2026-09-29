"""Customer orders.

Trust boundary: every price on an Order/OrderItem is computed server-side
from the current MenuItem row at checkout time (see app/services/orders.py)
and then frozen as a snapshot. A client-submitted price, subtotal, or total
is never accepted as authoritative anywhere in this module or its callers.
Money is ``Numeric`` end to end, never ``float`` -- same rule as
``MenuItem.price`` (app/models/menu_item.py).

``OrderStatus`` already carries the full restaurant lifecycle even though
this milestone only ever writes ``PENDING`` and exposes no status-change
route -- so Milestone 05's staff status-management feature is a new
endpoint on top of an already-correct column, not a schema change.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import enum_column


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    READY = "ready"
    SERVED = "served"  # dine-in: food at the table, bill not yet settled
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class OrderSource(str, enum.Enum):
    """Where an order came from. Only DINE_IN carries a table -- enforced by
    ``ck_orders_source_table`` below as well as in the checkout service."""

    DINE_IN = "dine_in"
    TAKEAWAY = "takeaway"
    DELIVERY = "delivery"
    ONLINE = "online"


class CancellationActor(str, enum.Enum):
    """Who cancelled. Recorded from the acting account's server-side role,
    never from anything the client sent."""

    CUSTOMER = "customer"
    STAFF = "staff"
    ADMIN = "admin"


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        sa.CheckConstraint("subtotal >= 0", name="ck_orders_subtotal_nonnegative"),
        sa.CheckConstraint("total >= 0", name="ck_orders_total_nonnegative"),
        # Backs "my orders, newest first" -- the only query GET /orders runs.
        sa.Index("ix_orders_user_created", "user_id", "created_at"),
        # Backs the date-range scans in app/services/sales.py.
        sa.Index("ix_orders_created_at", "created_at"),
        # A dine-in order always names its table; no other source ever does.
        sa.CheckConstraint(
            "(source = 'dine_in' AND table_id IS NOT NULL) OR (source <> 'dine_in' AND table_id IS NULL)",
            name="ck_orders_source_table",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # RESTRICT, not CASCADE/SET NULL: an order is a financial record and
    # must never be silently orphaned or deleted as a side effect of
    # something happening to the account. There is no user-delete route in
    # this codebase, so this is a documented invariant, not yet exercised.
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[OrderStatus] = mapped_column(
        enum_column(OrderStatus, 16, name="ck_orders_status"),
        nullable=False,
        server_default=OrderStatus.PENDING.value,
    )

    subtotal: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)
    # Equal to subtotal in this milestone -- no tax, discount, or delivery
    # fee exists yet. Kept as its own column (rather than derived) because
    # the brief requires it and a future fee/discount milestone needs a
    # place to diverge from subtotal without a schema change.
    total: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)

    # Orders placed before sources existed were all web checkouts, hence the
    # ONLINE default (also what the migration backfills).
    source: Mapped[OrderSource] = mapped_column(
        enum_column(OrderSource, 16, name="ck_orders_source"),
        nullable=False,
        server_default=OrderSource.ONLINE.value,
    )
    # RESTRICT for the same reason as user_id: a table with order history can
    # be taken out of service but never deleted out from under its orders.
    table_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("restaurant_tables.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # Cancellation record. All four stay NULL unless status is CANCELLED
    # (and for orders cancelled before these columns existed).
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    cancelled_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancellation_actor: Mapped[CancellationActor | None] = mapped_column(
        enum_column(CancellationActor, 16, name="ck_orders_cancellation_actor"), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    # Read-only convenience for the staff queue, which shows *whose* order
    # each row is. Deliberately exposes the account, not a copy of any
    # personal detail -- the template renders username only.
    customer: Mapped["User"] = relationship(foreign_keys=[user_id])  # noqa: F821
    table: Mapped["RestaurantTable | None"] = relationship()  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Order id={self.id} user_id={self.user_id} status={self.status.value} total={self.total}>"


class OrderItem(db.Model):
    __tablename__ = "order_items"
    __table_args__ = (
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price_snapshot >= 0", name="ck_order_items_unit_price_nonnegative"),
        sa.CheckConstraint("line_total >= 0", name="ck_order_items_line_total_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    order_id: Mapped[int] = mapped_column(
        sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT: menu items are never deleted in this codebase (only
    # deactivated via is_available), and a historical order line must never
    # be allowed to lose its reference even if that changes later.
    menu_item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("menu_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Frozen at checkout time (app/services/orders.py) -- a later menu item
    # rename or price change must never rewrite a historical order.
    item_name_snapshot: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)

    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    menu_item: Mapped["MenuItem"] = relationship()  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<OrderItem id={self.id} order_id={self.order_id} qty={self.quantity}>"
