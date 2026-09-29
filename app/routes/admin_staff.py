"""Admin approval of staff accounts.

ADMIN only, enforced server-side by ``require_role``. State changes are
POST-only and CSRF-protected by the application-wide ``CSRFProtect``. The
account to change comes from the URL's ``<int:...>`` converter and is
re-fetched and re-validated here: it must exist and must be a STAFF account,
so this page can never be used to change a customer or an admin.

Approval takes effect on the staff member's next request and revocation
likewise -- ``get_current_user`` re-reads ``staff_approved`` every time, so a
revoked account's existing session stops working immediately.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, url_for

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.user import Role, User
from app.services.audit import record_event
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

admin_staff_bp = Blueprint("admin_staff", __name__, url_prefix="/admin/staff")


@admin_staff_bp.get("")
@require_role(Role.ADMIN)
def staff_list():
    staff = db.session.query(User).filter_by(role=Role.STAFF).order_by(User.created_at, User.id).all()
    return render_template(
        "admin/staff_list.html",
        pending=[u for u in staff if not u.staff_approved],
        approved=[u for u in staff if u.staff_approved],
    )


def _set_approval(user_id: int, approved: bool):
    user = db.session.get(User, user_id)
    if user is None or user.role != Role.STAFF:
        abort(404)

    if user.staff_approved != approved:
        user.staff_approved = approved
        db.session.commit()
        record_event(
            AuditEvent.STAFF_APPROVED if approved else AuditEvent.STAFF_APPROVAL_REVOKED,
            success=True,
            user_id=get_current_user().id,
            ip_address=client_ip(),
            user_agent=user_agent(),
            metadata={"staff_user_id": user.id},
        )
    if approved:
        flash(f"{user.username} is approved and can now sign in to the staff portal.", "success")
    else:
        flash(f"Staff access for {user.username} has been revoked.", "success")
    return redirect(url_for("admin_staff.staff_list"))


@admin_staff_bp.post("/<int:user_id>/approve")
@require_role(Role.ADMIN)
def approve(user_id: int):
    return _set_approval(user_id, True)


@admin_staff_bp.post("/<int:user_id>/revoke")
@require_role(Role.ADMIN)
def revoke(user_id: int):
    return _set_approval(user_id, False)
