"""Sales dashboard and statements -- ADMIN only.

Read-only: every figure is derived on request from ``orders`` by
``app/services/sales.py``. GET with query-string filters, so a statement is
bookmarkable (``/admin/reports?month=2026-09``).
"""

from __future__ import annotations

from flask import Blueprint, flash, render_template, request

from app.models.restaurant_table import TableStatus
from app.models.user import Role
from app.services.sales import PRESETS, ReportRangeError, build_report, db_today, resolve_range, totals_for
from app.services.tables import list_tables
from app.utils.authorization import require_role

reports_bp = Blueprint("reports", __name__, url_prefix="/admin/reports")


@reports_bp.get("")
@require_role(Role.ADMIN)
def sales():
    args = request.args
    try:
        start, end, label = resolve_range(args.get("range"), args.get("start"), args.get("end"), args.get("month"))
    except ReportRangeError as exc:
        for messages in exc.errors.values():
            for message in messages:
                flash(message, "error")
        start, end, label = resolve_range("month")

    today = db_today()
    tables = list_tables()
    return render_template(
        "reports/sales.html",
        report=build_report(start, end, label),
        today_totals=totals_for(today, today),
        month_totals=totals_for(today.replace(day=1), today),
        table_counts={status: sum(t.status == status for t in tables) for status in TableStatus},
        presets=PRESETS,
        selected=args.get("range") or ("" if args.get("month") else "month"),
        args=args,
    )
