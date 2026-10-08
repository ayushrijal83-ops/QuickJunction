"""The role dashboard (``/account/``), self-service password change
(``/account/password``) and one-time code redemption (``/account/setup``,
M4 -- staff invitations and admin-issued resets).

The M02 probe endpoints (``/account/me``, ``/account/staff``,
``/account/admin``) were removed in M2: they were test scaffolding, not
product. The tests use an equivalent test-only blueprint (tests/conftest.py).
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, url_for

from app.models.account_token import TokenPurpose
from app.models.audit_log import AuditEvent
from app.models.user import Role
from app.routes.forms import AccountSetupForm, PasswordChangeForm
from app.services import dashboard
from app.services.accounts import AccountError, CodeRejected, change_password, redeem_code
from app.services.audit import record_event
from app.utils.authorization import get_current_user, login_required, login_user
from app.utils.ratelimit import login_limiter
from app.utils.request_meta import client_ip, user_agent

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


def _form_errors(form, errors: dict[str, list[str]]) -> None:
    for field_name, messages in errors.items():
        target = getattr(form, field_name, None)
        if target is not None and hasattr(target, "errors"):
            target.errors = list(target.errors) + messages
        else:
            for message in messages:
                flash(message, "error")


@account_bp.route("/password", methods=["GET", "POST"])
@login_required
def password():
    """Change your own password. Wrong current passwords are counted by the
    login limiter (keyed per account), so a stolen session cannot be used to
    brute-force the password. Success revokes every other session of this
    account and keeps this one signed in."""
    user = get_current_user()
    form = PasswordChangeForm()
    if form.validate_on_submit():
        key = f"password-change:{user.id}"
        if login_limiter.is_limited(key):
            flash("Too many attempts. Try again later.", "error")
            return render_template("account/password.html", form=form), 429
        try:
            change_password(user, form.current_password.data, form.password.data, form.password_confirm.data,
                            audit=lambda u: record_event(AuditEvent.PASSWORD_CHANGED, success=True, user_id=u.id,
                                                         ip_address=client_ip(), user_agent=user_agent()))
        except AccountError as exc:
            if "current_password" in exc.errors:
                login_limiter.record_failure(key)
                record_event(AuditEvent.PASSWORD_CHANGED, success=False, user_id=user.id, ip_address=client_ip(),
                             user_agent=user_agent(), metadata={"reason": "wrong_current_password"}, commit=True)
            _form_errors(form, exc.errors)
        else:
            login_limiter.reset(key)
            login_user(user)  # re-stamp this session with the new version
            flash("Password changed. You have been signed out everywhere else.", "success")
            return redirect(url_for("account.index"))
    return render_template("account/password.html", form=form)


@account_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Redeem a one-time code from an administrator: accept a staff
    invitation or complete a password reset. Public by necessity; the code
    itself (256 random bits, single use, expiring) is the credential."""
    if get_current_user() is not None:
        flash("Sign out before using a one-time code.", "error")
        return redirect(url_for("account.index"))
    form = AccountSetupForm()
    if form.validate_on_submit():
        def audit(user, purpose):
            event = (AuditEvent.STAFF_INVITATION_ACCEPTED if purpose == TokenPurpose.STAFF_INVITE
                     else AuditEvent.PASSWORD_RESET)
            return record_event(event, success=True, user_id=user.id, ip_address=client_ip(), user_agent=user_agent())

        try:
            user, purpose = redeem_code(form.code.data, form.password.data, form.password_confirm.data, audit=audit)
        except CodeRejected as exc:
            record_event(AuditEvent.ACCOUNT_TOKEN_REJECTED, success=False, ip_address=client_ip(),
                         user_agent=user_agent(),
                         metadata={"purpose": exc.purpose.value} if exc.purpose else None, commit=True)
            _form_errors(form, exc.errors)
        except AccountError as exc:
            _form_errors(form, exc.errors)
        else:
            portal = "staff" if user.role == Role.STAFF else ("admin" if user.role == Role.ADMIN else "customer")
            flash("Your password is set. Sign in with it now.", "success")
            return redirect(url_for("auth.login", portal=portal))
    return render_template("account/setup.html", form=form)
