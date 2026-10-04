"""The role dashboard (``/account/``).

The M02 probe endpoints (``/account/me``, ``/account/staff``,
``/account/admin``) were removed in M2: they were test scaffolding, not
product. The tests use an equivalent test-only blueprint (tests/conftest.py).
"""

from __future__ import annotations

from flask import Blueprint, render_template

from app.models.user import Role
from app.services import dashboard
from app.utils.authorization import get_current_user, login_required

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

