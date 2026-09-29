"""The role dashboard (``/account/``) plus the small authorization-foundation
endpoints (``/account/me``, ``/account/staff``, ``/account/admin``) that the
M02 role-boundary tests exercise.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from app.models.user import Role
from app.services import dashboard
from app.utils.authorization import get_current_user, login_required, require_role

account_bp = Blueprint("account", __name__, url_prefix="/account")


@account_bp.get("/")
@login_required
def index():
    """Role-aware dashboard every login redirects to (M15): the customer,
    staff and admin views are built by app/services/dashboard.py, whose data
    scope follows RBAC (customer: own data only; staff: operational counts,
    no money; admin: the business view)."""
    user = get_current_user()
    data = {Role.CUSTOMER: dashboard.customer, Role.STAFF: dashboard.staff, Role.ADMIN: dashboard.admin}
    context = data[user.role](user) if user.role == Role.CUSTOMER else data[user.role]()
    return render_template(f"dashboard/{user.role.value}.html", user=user, **context)


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
