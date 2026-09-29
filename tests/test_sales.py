"""Milestone 10 (brief M08): sales reporting derived from orders."""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from flask import g

from app.extensions import db as _db
from app.models.order import Order, OrderSource, OrderStatus
from app.models.user import Role
from app.services.sales import ReportRangeError, build_report, db_today, resolve_range, totals_for
from app.services.tables import create_table
from app.services.orders import checkout
from tests.conftest import make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"
_seq = itertools.count()
D = Decimal


@pytest.fixture
def buyer(db):
    return make_user(username="buyer", email="buyer@example.com", password=PASSWORD)


def order(user, price: str, qty: int = 1, status=OrderStatus.COMPLETED, when: datetime | None = None,
          source=OrderSource.ONLINE, table_id=None) -> Order:
    category = make_category(name=f"Cat {next(_seq)}")
    item = make_menu_item(category=category, name=f"Dish {category.name}", price=price)
    placed = checkout(user.id, {str(item.id): qty}, source, table_id)
    placed.status = status
    if when is not None:
        placed.created_at = when
    _db.session.commit()
    return placed


def at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour)


def test_cancelled_and_unfinished_orders_are_not_sales(buyer):
    """The brief's example: 1,000 + 1,500 completed, 2,000 cancelled -> 2,500."""
    today = db_today()
    order(buyer, "1000.00", when=at(today))
    order(buyer, "1500.00", when=at(today))
    order(buyer, "2000.00", status=OrderStatus.CANCELLED, when=at(today))
    order(buyer, "700.00", status=OrderStatus.PREPARING, when=at(today))

    report = build_report(today, today, "Today")
    t = report.totals
    assert (t.orders, t.gross, t.net, t.collected) == (2, D("2500.00"), D("2500.00"), D("2500.00"))
    assert (t.discounts, t.tax, t.refunds) == (D("0.00"),) * 3
    assert t.average == D("1250.00")
    assert (report.placed, report.cancelled, report.active) == (4, 1, 1)


def test_daily_breakdown_and_date_filtering(buyer):
    d1, d2, outside = date(2026, 9, 1), date(2026, 9, 2), date(2026, 10, 1)
    order(buyer, "100.00", when=at(d1))
    order(buyer, "200.00", when=at(d2))
    order(buyer, "50.00", when=at(d2, 23))
    order(buyer, "999.00", when=at(outside))

    report = build_report(*resolve_range(None, month="2026-09"))
    assert report.label == "September 2026"
    assert len(report.daily) == 30
    by_day = {day: (placed, t.net) for day, placed, t in report.daily}
    assert by_day[d1] == (1, D("100.00"))
    assert by_day[d2] == (2, D("250.00"))
    assert report.totals.net == D("350.00")
    assert totals_for(outside, outside).net == D("999.00")


def test_range_presets(db):
    today = db_today()
    assert resolve_range("today")[:2] == (today, today)
    week_start, week_end, _ = resolve_range("week")
    assert week_start.weekday() == 0 and week_end == today
    assert resolve_range("month")[:2] == (today.replace(day=1), today)
    lm_start, lm_end, _ = resolve_range("last_month")
    assert lm_start.day == 1 and lm_end == today.replace(day=1) - timedelta(days=1)
    assert resolve_range("custom", "2026-01-05", "2026-01-07")[:2] == (date(2026, 1, 5), date(2026, 1, 7))


@pytest.mark.parametrize("kwargs", [
    {"preset": "custom", "start": "2026-02-10", "end": "2026-02-01"},
    {"preset": "custom", "start": "garbage", "end": "2026-02-01"},
    {"preset": "custom", "start": "2020-01-01", "end": "2026-01-01"},
    {"preset": "yesterday-ish"},
    {"preset": None, "month": "2026-13"},
])
def test_invalid_ranges_rejected(db, kwargs):
    with pytest.raises(ReportRangeError):
        resolve_range(**kwargs)


def test_hourly_aggregation(buyer):
    day = date(2026, 9, 3)
    order(buyer, "100.00", when=at(day, 12))
    order(buyer, "150.00", when=at(day, 12) + timedelta(minutes=40))
    order(buyer, "80.00", when=at(day, 19))
    hours = {h: (t.orders, t.net) for h, t in build_report(day, day, "x").by_hour}
    assert hours == {12: (2, D("250.00")), 19: (1, D("80.00"))}


def test_by_source_and_by_table(buyer):
    day = date(2026, 9, 4)
    t1, t2 = create_table("T01", 4), create_table("T02", 2)
    order(buyer, "300.00", when=at(day), source=OrderSource.DINE_IN, table_id=t1.id)
    order(buyer, "100.00", when=at(day), source=OrderSource.DINE_IN, table_id=t1.id)
    order(buyer, "100.00", when=at(day), source=OrderSource.TAKEAWAY)
    order(buyer, "500.00", status=OrderStatus.CANCELLED, when=at(day), source=OrderSource.DINE_IN, table_id=t2.id)

    report = build_report(day, day, "x")
    sources = {s: (t.orders, t.net, pct) for s, t, pct in report.by_source}
    assert sources[OrderSource.DINE_IN] == (2, D("400.00"), D("80.0"))
    assert sources[OrderSource.TAKEAWAY] == (1, D("100.00"), D("20.0"))
    assert sources[OrderSource.DELIVERY][0] == sources[OrderSource.ONLINE][0] == 0
    tables = {tbl.name: (t.orders, t.net) for tbl, t in report.by_table}
    assert tables == {"T01": (2, D("400.00")), "T02": (0, D("0.00"))}


def test_reports_use_historical_prices_not_current_menu(buyer):
    day = date(2026, 9, 5)
    placed = order(buyer, "350.00", when=at(day))
    placed.items[0].menu_item.price = D("400.00")
    _db.session.commit()
    assert placed.items[0].unit_price_snapshot == D("350.00")
    assert build_report(day, day, "x").totals.gross == D("350.00")


def test_empty_range_has_zero_average(db):
    report = build_report(date(2026, 1, 1), date(2026, 1, 1), "x")
    assert (report.totals.orders, report.totals.average) == (0, D("0.00"))


# --- route: RBAC + rendering ---------------------------------------------------


def _login(client, username, role):
    make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)
    g.pop("current_user", None)
    client.post("/login", data={"username": username, "password": PASSWORD})


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.STAFF])
def test_only_admin_sees_sales(client, db, role):
    assert client.get("/admin/reports").status_code == 401
    _login(client, "someone", role)
    assert client.get("/admin/reports").status_code == 403


def test_admin_dashboard_and_statement_render(client, buyer):
    order(buyer, "1234.00", when=at(date(2026, 8, 15)))
    _login(client, "boss", Role.ADMIN)
    body = client.get("/admin/reports?month=2026-08").get_data(as_text=True)
    assert "August 2026" in body and "1234.00" in body
    # Revenue is never presented as profit (no cost data exists).
    assert "Revenue only" in body and "Profit" not in body
    assert client.get("/admin/reports?range=custom&start=2026-08-01&end=2026-08-31").status_code == 200
    # A bad filter falls back to this month with a message, never a 500.
    assert client.get("/admin/reports?range=custom&start=bad").status_code == 200


def test_midnight_and_month_boundaries(buyer):
    """Ranges are [start 00:00, day-after-end 00:00): 23:59:59 on the last day
    belongs to that month, 00:00:00 the next day does not."""
    last_of_sept = datetime(2026, 9, 30, 23, 59, 59)
    order(buyer, "10.00", when=last_of_sept)
    order(buyer, "20.00", when=datetime(2026, 10, 1, 0, 0, 0))
    order(buyer, "40.00", when=datetime(2026, 9, 1, 0, 0, 0))
    assert build_report(*resolve_range(None, month="2026-09")).totals.net == D("50.00")
    assert build_report(*resolve_range(None, month="2026-10")).totals.net == D("20.00")
    assert totals_for(date(2026, 9, 30), date(2026, 9, 30)).net == D("10.00")


def test_served_orders_are_pending_not_sales(buyer):
    day = date(2026, 9, 6)
    order(buyer, "300.00", status=OrderStatus.SERVED, when=at(day))
    report = build_report(day, day, "x")
    assert (report.totals.orders, report.active) == (0, 1)
