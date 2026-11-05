"""Read-only admin views (M15): payments & refunds, and the audit log.

ADMIN only (``require_role``); GET only -- nothing here changes state.
Refunds are still made on the order page (``/staff/orders/<id>``), through
the staff order routes. The audit view renders stored metadata as escaped
text; ``record_event`` already refuses secret-bearing keys, so no credential
can appear here.

M7.5.3: this list reads ``payment_ledger_entries`` (the source of truth for
every payment recorded since the cutover -- see
app/services/payment_ledger.py), not the legacy ``payments`` table, which
stops receiving new rows once the route is cut over. A refund entry does
not carry its own method or its reason on the payment row the way the
legacy schema did (a payment can have more than one refund, each with its
own reason), so refund reasons are looked up separately below and shown as
the most recent one per payment.
"""

from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, render_template, request
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.models.user import Role, User
from app.utils.authorization import require_role

admin_views_bp = Blueprint("admin_views", __name__, url_prefix="/admin")

PAGE_SIZE = 200


@admin_views_bp.get("/payments")
@require_role(Role.ADMIN)
def payments():
    rows = (
        db.session.query(PaymentLedgerEntry)
        .filter(PaymentLedgerEntry.entry_type == LedgerEntryType.PAYMENT)
        .options(selectinload(PaymentLedgerEntry.order).selectinload(Order.table),
                 # _payment_badge.html falls back to order.payment for a
                 # fully-refunded row's own badge; eager-load it so that
                 # does not become one extra query per row.
                 selectinload(PaymentLedgerEntry.order).selectinload(Order.payment))
        .order_by(PaymentLedgerEntry.created_at.desc(), PaymentLedgerEntry.id.desc())
        .limit(PAGE_SIZE)
        .all()
    )
    payment_ids = [p.id for p in rows]
    refund_reasons: dict[int, str | None] = {}
    if payment_ids:
        refund_rows = (
            db.session.query(PaymentLedgerEntry)
            .filter(PaymentLedgerEntry.entry_type == LedgerEntryType.REFUND,
                     PaymentLedgerEntry.reversed_entry_id.in_(payment_ids))
            .order_by(PaymentLedgerEntry.created_at.asc())
            .all()
        )
        for r in refund_rows:
            refund_reasons[r.reversed_entry_id] = r.reason  # ascending order -- last write is the most recent
    staff = {u.id: u.username for u in db.session.query(User).filter(User.id.in_({p.recorded_by_id for p in rows}))}
    return render_template(
        "admin/payments.html",
        payments=rows,
        staff=staff,
        refund_reasons=refund_reasons,
        collected=sum((p.amount for p in rows), start=Decimal("0.00")),
        refunded=sum((p.refunded_amount for p in rows), start=Decimal("0.00")),
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
