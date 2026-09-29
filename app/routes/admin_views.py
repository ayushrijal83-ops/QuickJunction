"""Read-only admin views (M15): payments & refunds, and the audit log.

ADMIN only (``require_role``); GET only -- nothing here changes state.
Refunds are still made on the order page (``/staff/orders/<id>``), through
the existing M11 route. The audit view renders stored metadata as escaped
text; ``record_event`` already refuses secret-bearing keys, so no credential
can appear here.
"""

from __future__ import annotations

from flask import Blueprint, render_template, request
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import Role, User
from app.utils.authorization import require_role

admin_views_bp = Blueprint("admin_views", __name__, url_prefix="/admin")

PAGE_SIZE = 200


@admin_views_bp.get("/payments")
@require_role(Role.ADMIN)
def payments():
    rows = (
        db.session.query(Payment)
        .options(selectinload(Payment.order).selectinload(Order.table))
        .order_by(Payment.captured_at.desc(), Payment.id.desc())
        .limit(PAGE_SIZE)
        .all()
    )
    staff = {u.id: u.username for u in db.session.query(User).filter(User.id.in_({p.recorded_by_id for p in rows}))}
    return render_template(
        "admin/payments.html",
        payments=rows,
        staff=staff,
        collected=sum((p.amount for p in rows), start=0),
        refunded=sum((p.refunded_amount for p in rows), start=0),
    )


@admin_views_bp.get("/audit")
@require_role(Role.ADMIN)
def audit():
    raw = request.args.get("event", "")
    event = raw if raw in {e.value for e in AuditEvent} else ""  # allow-list, never raw SQL
    query = db.session.query(AuditLog)
    if event:
        query = query.filter(AuditLog.event_type == AuditEvent(event))
    rows = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(PAGE_SIZE).all()
    users = {u.id: u.username for u in db.session.query(User).filter(User.id.in_({r.user_id for r in rows if r.user_id}))}
    return render_template("admin/audit.html", rows=rows, users=users, events=sorted(e.value for e in AuditEvent),
                           selected=event)
