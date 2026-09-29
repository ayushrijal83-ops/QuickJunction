"""Role dashboards (M15) -- composition only.

Every figure comes from an existing service (sales, kitchen, tables,
reservations, inventory, orders, recommendations); this module adds no new
query logic and no new money arithmetic. Data scope follows RBAC:

- customer: own orders and reservations only (every read scoped to user.id);
- staff: operational counts -- kitchen, tables, today's reservations, low
  stock, today's order *counts*. No money: sales figures are ADMIN-only
  (``/admin/reports``), and the dashboard does not widen that;
- admin: the business view, including sales from ``app/services/sales``.
"""

from __future__ import annotations

from app.extensions import db
from app.models.order import OrderStatus
from app.models.reservation import ReservationStatus
from app.models.restaurant_table import TableStatus
from app.models.user import Role, User
from app.services.inventory import list_ingredients
from app.services.kitchen import queue
from app.services.orders import CUSTOMER_CANCELLABLE_STATUSES, list_orders_for_user
from app.services.preferences import get_preferences
from app.services.recommendations import recommend_for_user
from app.services.reservations import list_reservations_for_staff, list_reservations_for_user
from app.services.sales import build_report, totals_for
from app.services.tables import list_tables
from app.utils.clock import db_today

_FINISHED = (OrderStatus.COMPLETED, OrderStatus.CANCELLED)


def _table_counts() -> tuple[list, dict[TableStatus, int]]:
    tables = list_tables()
    return tables, {status: sum(t.status == status for t in tables) for status in TableStatus}


def customer(user: User) -> dict:
    today = db_today()
    orders = list_orders_for_user(user.id)
    tables, _ = _table_counts()
    return {
        "active_order": next((o for o in orders if o.status not in _FINISHED), None),
        "recent_orders": orders[:5],
        "cancellable": CUSTOMER_CANCELLABLE_STATUSES,
        "upcoming": [r for r in list_reservations_for_user(user.id)
                     if r.reservation_date >= today and r.status == ReservationStatus.CONFIRMED][::-1][:4],
        "free_tables": [t for t in tables if t.status == TableStatus.AVAILABLE],
        "top_picks": recommend_for_user(user.id, get_preferences(user.id))[:3],
    }


def staff() -> dict:
    today = db_today()
    tables, table_counts = _table_counts()
    report = build_report(today, today, "Today")
    return {
        "kitchen": {status: len(orders) for status, orders in queue().items()},
        "tables": tables,
        "table_counts": table_counts,
        "todays_reservations": [r for r in list_reservations_for_staff(today)
                                if r.reservation_date == today and r.status == ReservationStatus.CONFIRMED],
        "low_stock": [i for i in list_ingredients() if i.is_low],
        "orders_today": report.placed,
        "completed_today": report.totals.orders,
    }


def admin() -> dict:
    today = db_today()
    data = staff()
    report = build_report(today, today, "Today")
    data.update(
        today=report,
        month=totals_for(today.replace(day=1), today),
        pending_staff=db.session.query(User).filter_by(role=Role.STAFF, staff_approved=False).count(),
    )
    return data
