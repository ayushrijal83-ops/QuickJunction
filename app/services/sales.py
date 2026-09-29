"""Sales reporting, derived from the transactional ``orders`` + ``payments`` rows.

There is no sales table: every figure is recomputed from ``Order`` and its
``Payment`` on each request, so a report can never disagree with the records
it describes. Money comes only from the order's own ``subtotal`` (the
checkout-time snapshot, never current menu prices) and the payment row.

Financial definitions (these are SALES/REVENUE, not profit -- Quick Junction
records no costs, so profit cannot be computed):

- **Recognised sale** -- an order with status ``COMPLETED`` (served/handed
  over). Pending and in-progress orders are not sales yet; cancelled orders
  never are -- and cannot hold money, since a paid order must be refunded in
  full before it can be cancelled.
- **Gross sales** -- sum of ``subtotal`` over recognised sales.
- **Discounts** -- sum of the order's ``discount_amount`` snapshot (M12).
- **Tax** -- sum of the order's ``tax_amount`` snapshot (M12), at the rate
  each order was charged -- never recomputed from the current setting. Tax is
  collected on behalf of the tax authority, so it is not part of net sales.
- **Refunds** -- sum of ``payments.refunded_amount`` over recognised sales.
- **Net sales** -- gross - discounts - refunds (excludes tax).
- **Paid orders** -- recognised sales that have a payment row.
- **Collected** -- sum of ``amount - refunded_amount`` of the payments on
  recognised sales. ``Report.prepaid`` separately shows money already taken
  for orders that are not completed yet (e.g. takeaway paid at the counter).
- **Unpaid completed** -- recognised sales with no payment: money owed.
- **Average order value** -- net sales / recognised order count.

Dates are the order's ``created_at`` (when it was placed), compared against
the database's own clock, so "today" always means the same day the database
stamped on the order.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.extensions import db
from app.models.order import Order, OrderSource, OrderStatus
from app.models.payment import Payment
from app.models.restaurant_table import RestaurantTable
from app.services.errors import ValidationError
from app.services.tables import ACTIVE_ORDER_STATUSES
from app.utils.clock import db_today

ZERO = Decimal("0.00")
MAX_RANGE_DAYS = 366
PRESETS = ("today", "week", "month", "last_month", "custom")


class ReportRangeError(ValidationError):
    pass


@dataclass
class Totals:
    orders: int = 0  # recognised (completed) orders
    gross: Decimal = ZERO
    discounts: Decimal = ZERO
    tax: Decimal = ZERO
    refunds: Decimal = ZERO
    collected: Decimal = ZERO
    paid: int = 0  # recognised orders with a payment

    @property
    def net(self) -> Decimal:
        return self.gross - self.discounts - self.refunds

    @property
    def average(self) -> Decimal:
        return (self.net / self.orders).quantize(Decimal("0.01")) if self.orders else ZERO

    def add(self, row) -> None:
        self.orders += 1
        self.gross += row.subtotal
        self.discounts += row.discount_amount
        self.tax += row.tax_amount
        if row.amount is not None:
            self.paid += 1
            self.refunds += row.refunded_amount
            self.collected += row.amount - row.refunded_amount


@dataclass
class Report:
    start: date
    end: date  # inclusive
    label: str
    totals: Totals = field(default_factory=Totals)
    placed: int = 0
    cancelled: int = 0
    active: int = 0
    unpaid_completed: int = 0
    prepaid: Decimal = ZERO  # net payments on orders not completed yet
    daily: list[tuple[date, int, Totals]] = field(default_factory=list)
    by_source: list[tuple[OrderSource, Totals, Decimal]] = field(default_factory=list)
    by_table: list[tuple[RestaurantTable, Totals]] = field(default_factory=list)
    by_hour: list[tuple[int, Totals]] = field(default_factory=list)


def resolve_range(preset: str | None, start: str | None = None, end: str | None = None,
                  month: str | None = None) -> tuple[date, date, str]:
    """Turn query-string input into an inclusive (start, end, label).
    ``month=YYYY-MM`` (a monthly statement) takes precedence over presets."""
    today = db_today()
    if month:
        try:
            first = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise ReportRangeError({"month": ["Month must look like 2026-09."]})
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        return first, last, first.strftime("%B %Y")

    preset = preset or "month"
    if preset == "today":
        return today, today, "Today"
    if preset == "week":
        monday = today - timedelta(days=today.weekday())
        return monday, today, "This week"
    if preset == "month":
        return today.replace(day=1), today, today.strftime("%B %Y") + " (to date)"
    if preset == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last, last.strftime("%B %Y")
    if preset == "custom":
        try:
            first, last = date.fromisoformat(start or ""), date.fromisoformat(end or "")
        except ValueError:
            raise ReportRangeError({"range": ["Choose a valid start and end date."]})
        if first > last:
            raise ReportRangeError({"range": ["The start date must be on or before the end date."]})
        if (last - first).days >= MAX_RANGE_DAYS:
            raise ReportRangeError({"range": [f"Choose a range of at most {MAX_RANGE_DAYS} days."]})
        return first, last, f"{first:%d %b %Y} – {last:%d %b %Y}"
    raise ReportRangeError({"range": ["Unknown date range."]})


def _rows(start: date, end: date):
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time())
    # ponytail: aggregates in Python over one column-only query; move to SQL
    # GROUP BY if a single report ever spans 100k+ orders.
    return db.session.execute(
        sa.select(Order.status, Order.created_at, Order.subtotal, Order.discount_amount, Order.tax_amount,
                  Order.source, Order.table_id, Payment.amount, Payment.refunded_amount)
        .outerjoin(Payment, Payment.order_id == Order.id)
        .where(Order.created_at >= lo, Order.created_at < hi)
    ).all()


def totals_for(start: date, end: date) -> Totals:
    totals = Totals()
    for row in _rows(start, end):
        if row.status == OrderStatus.COMPLETED:
            totals.add(row)
    return totals


def build_report(start: date, end: date, label: str) -> Report:
    report = Report(start=start, end=end, label=label)
    daily: dict[date, Totals] = defaultdict(Totals)
    placed_per_day: dict[date, int] = defaultdict(int)
    sources: dict[OrderSource, Totals] = defaultdict(Totals)
    tables: dict[int, Totals] = defaultdict(Totals)
    hours: dict[int, Totals] = defaultdict(Totals)

    for row in _rows(start, end):
        report.placed += 1
        placed_per_day[row.created_at.date()] += 1
        if row.status == OrderStatus.CANCELLED:
            report.cancelled += 1
        elif row.status in ACTIVE_ORDER_STATUSES:
            report.active += 1
        if row.status != OrderStatus.COMPLETED:
            if row.amount is not None:
                report.prepaid += row.amount - row.refunded_amount
            continue
        if row.amount is None:
            report.unpaid_completed += 1
        for bucket in (report.totals, daily[row.created_at.date()], sources[row.source], hours[row.created_at.hour]):
            bucket.add(row)
        if row.table_id is not None:
            tables[row.table_id].add(row)

    day = start
    while day <= end:
        report.daily.append((day, placed_per_day[day], daily[day]))
        day += timedelta(days=1)

    net = report.totals.net
    report.by_source = [
        (source, sources[source], (sources[source].net * 100 / net).quantize(Decimal("0.1")) if net else ZERO)
        for source in OrderSource
    ]
    report.by_table = [(table, tables[table.id]) for table in db.session.query(RestaurantTable).order_by(RestaurantTable.name)]
    report.by_hour = sorted(hours.items())
    return report
