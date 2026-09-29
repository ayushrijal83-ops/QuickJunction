"""Authorization-foundation endpoints.

Not business features -- there is no dashboard yet. These exist to give the
role decorators something real to protect, for the manual browser test and
for tests 11-13 (customer/staff/admin boundaries). Later milestones replace
them with actual staff and admin views.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from app.extensions import db
from app.models.restaurant_table import TableStatus
from app.models.user import Role, User
from app.services.orders import CUSTOMER_CANCELLABLE_STATUSES, list_orders_for_user
from app.services.reservations import list_reservations_for_user
from app.services.tables import list_tables
from app.utils.clock import db_today
from app.utils.authorization import get_current_user, login_required, require_role

account_bp = Blueprint("account", __name__, url_prefix="/account")


@account_bp.get("/")
@login_required
def index():
    """Role-aware landing page every login redirects to. For a customer it is
    the customer dashboard (recent orders, upcoming reservations, free
    tables); staff and admin keep their existing tiles."""
    user = get_current_user()
    pending_staff = 0
    if user.role == Role.ADMIN:
        pending_staff = (
            db.session.query(User).filter_by(role=Role.STAFF, staff_approved=False).count()
        )
    customer = {}
    if user.role == Role.CUSTOMER:
        # The customer dashboard: own data only, every query scoped to user.id.
        today = db_today()
        customer = {
            "recent_orders": list_orders_for_user(user.id)[:5],
            "cancellable": CUSTOMER_CANCELLABLE_STATUSES,
            "upcoming": [r for r in list_reservations_for_user(user.id) if r.reservation_date >= today][::-1][:5],
            "free_tables": [t for t in list_tables() if t.status == TableStatus.AVAILABLE],
        }
    return render_template("account.html", user=user, pending_staff=pending_staff, **customer)


@account_bp.get("/me")
@login_required
def me():
    user = get_current_user()
    return jsonify(username=user.username, role=user.role.value)


@account_bp.get("/staff")
@require_role(Role.STAFF, Role.ADMIN)
def staff_only():
    return jsonify(message="staff access confirmed")


@account_bp.get("/admin")
@require_role(Role.ADMIN)
def admin_only():
    return jsonify(message="admin access confirmed")
