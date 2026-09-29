"""Kitchen view (M14): the existing order lifecycle, seen from the pass.

No new statuses and no new write path: the queue is a read of orders in
PENDING / CONFIRMED / PREPARING / READY, and every action goes through
``update_order_status`` (the atomic conditional UPDATE), so the M10 race
guarantees -- never PREPARING + CANCELLED -- hold unchanged. Timings use the
``preparing_at`` / ``ready_at`` stamps that transition writes, measured
against the database clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.order import Order, OrderStatus

KITCHEN_COLUMNS = (OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY)

# The one forward step the kitchen takes from each column. Everything else
# (cancel, serve, complete) stays on the full staff order page.
KITCHEN_NEXT = {
    OrderStatus.PENDING: OrderStatus.CONFIRMED,
    OrderStatus.CONFIRMED: OrderStatus.PREPARING,
    OrderStatus.PREPARING: OrderStatus.READY,
}


@dataclass
class KitchenStats:
    prepared_today: int
    average_prep: timedelta | None
    longest_active: Order | None


def queue() -> dict[OrderStatus, list[Order]]:
    """Active orders by column, oldest first -- items and table eager-loaded
    (one query each, however long the queue)."""
    orders = (
        db.session.query(Order)
        .options(selectinload(Order.items), selectinload(Order.table))
        .filter(Order.status.in_(KITCHEN_COLUMNS))
        .order_by(Order.created_at, Order.id)
        .all()
    )
    columns: dict[OrderStatus, list[Order]] = {status: [] for status in KITCHEN_COLUMNS}
    for order in orders:
        columns[order.status].append(order)
    return columns


def stats(columns: dict[OrderStatus, list[Order]], today: date) -> KitchenStats:
    start = datetime.combine(today, datetime.min.time())
    done = (
        db.session.query(Order.preparing_at, Order.ready_at)
        .filter(Order.ready_at >= start, Order.preparing_at.isnot(None))
        .all()
    )
    durations = [ready - started for started, ready in done if ready >= started]
    average = sum(durations, timedelta()) / len(durations) if durations else None
    active = [o for orders in columns.values() for o in orders]
    return KitchenStats(
        prepared_today=len(done),
        average_prep=average,
        longest_active=min(active, key=lambda o: (o.created_at, o.id)) if active else None,
    )


def minutes(delta: timedelta | None) -> int | None:
    return None if delta is None else max(0, int(delta.total_seconds() // 60))
