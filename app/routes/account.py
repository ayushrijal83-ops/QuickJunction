"""Authorization-foundation endpoints.

Not business features -- there is no dashboard yet. These exist to give the
role decorators something real to protect, for the manual browser test and
for tests 11-13 (customer/staff/admin boundaries). Later milestones replace
them with actual staff and admin views.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from app.models.user import Role
from app.utils.authorization import get_current_user, login_required, require_role

account_bp = Blueprint("account", __name__, url_prefix="/account")


@account_bp.get("/")
@login_required
def index():
    """Minimal authenticated landing page -- exists so a logged-in browser
    user has somewhere with a (CSRF-protected) logout button, since there is
    no dashboard yet."""
    return render_template("account.html", user=get_current_user())


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
