"""Registration, login, logout.

Login failures are deliberately indistinguishable: the same message and
(via authenticate_user's dummy-hash comparison) roughly the same amount of
work whether the username doesn't exist or the password is wrong -- see
app/services/auth.py. Registration is the one place duplicate
username/email are named specifically; that is normal signup UX, not an
enumeration bug, and is documented as such in docs/SECURITY.md.
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.models.audit_log import AuditEvent
from app.models.user import Role
from app.routes.forms import LoginForm, RegistrationForm
from app.services.audit import record_event
from app.services.auth import RegistrationError, RegistrationInput, authenticate_user, register_user
from app.utils.authorization import get_current_user, login_required, login_user, logout_user
from app.utils.ratelimit import login_limiter
from app.utils.request_meta import client_ip as _client_ip
from app.utils.request_meta import user_agent as _user_agent

auth_bp = Blueprint("auth", __name__)

# Sign-in portals. One login view serves all three, so password checking,
# rate limiting and session creation exist exactly once. The portal chosen
# never grants anything: the role stored on the account decides access, and
# the staff/admin portals only *refuse* accounts of another role, so each
# portal is clearly for one kind of user. The customer portal (/login) is
# the general entry point and accepts every role, routing each account to
# its own dashboard.
PORTAL_ROLES = {"staff": Role.STAFF, "admin": Role.ADMIN}


def safe_next_target() -> str | None:
    """The ``?next=`` destination, but only if it is safe to follow.

    ``next`` arrives from the query string, so it is attacker-controlled: a
    crafted link such as ``/login?next=https://evil.example/`` would otherwise
    turn this application's own login page into an open redirect, which is a
    convincing phishing primitive precisely because the first hop is genuine.

    Only a path on this site is accepted. Everything else is discarded and the
    caller falls back to the default destination:

    * must start with a single ``/``  -- rejects absolute URLs
    * must not start with ``//``      -- rejects protocol-relative ``//evil``
    * must not contain a backslash    -- some browsers normalise ``\\`` to ``/``
    * must not be a login page        -- avoids a redirect loop

    Following it never bypasses authorization: the destination route runs
    its own role check like any other request.
    """
    target = request.args.get("next", "")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    path = urlparse(target).path
    login_path = url_for("auth.login")
    if path == login_path or path.startswith(login_path + "/"):
        return None
    return target


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user() is not None:
        return redirect(url_for("account.index"))

    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            user = register_user(
                RegistrationInput(
                    username=form.username.data,
                    email=form.email.data,
                    password=form.password.data,
                    password_confirm=form.password_confirm.data,
                )
            )
        except RegistrationError as exc:
            for field_name, messages in exc.errors.items():
                if hasattr(form, field_name):
                    getattr(form, field_name).errors.extend(messages)
                else:
                    for message in messages:
                        flash(message, "error")
        else:
            record_event(
                AuditEvent.REGISTER_SUCCESS,
                success=True,
                user_id=user.id,
                ip_address=_client_ip(),
                user_agent=_user_agent(),
            )
            # Sign the new account in rather than bouncing to the login form
            # with credentials they typed ten seconds ago. This is the same
            # login_user() the login view calls -- it clears any pre-existing
            # session first (fixation protection) and stamps the session
            # version, so the account is authenticated on exactly the same
            # terms as a normal login. Nothing about authorization changes:
            # register_user() assigns the role, and it is still CUSTOMER.
            login_user(user)
            flash("Welcome to Quick Junction. Tell us what you like.", "success")
            return redirect(url_for("preferences.preferences"))

    return render_template("register.html", form=form, staff=False)


@auth_bp.route("/register/staff", methods=["GET", "POST"])
def register_staff():
    """Staff sign-up. Creates a STAFF account that starts **pending**: it
    cannot sign in, holds no session and reaches no staff route until an
    admin approves it (app/routes/admin_staff.py). So, unlike customer
    registration, it deliberately does not sign the new account in."""
    if get_current_user() is not None:
        return redirect(url_for("account.index"))

    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            user = register_user(
                RegistrationInput(
                    username=form.username.data,
                    email=form.email.data,
                    password=form.password.data,
                    password_confirm=form.password_confirm.data,
                ),
                as_staff=True,
            )
        except RegistrationError as exc:
            for field_name, messages in exc.errors.items():
                if hasattr(form, field_name):
                    getattr(form, field_name).errors.extend(messages)
                else:
                    for message in messages:
                        flash(message, "error")
        else:
            record_event(
                AuditEvent.STAFF_REGISTERED,
                success=True,
                user_id=user.id,
                ip_address=_client_ip(),
                user_agent=_user_agent(),
            )
            flash(
                "Staff account requested. An administrator must approve it before you can sign in.",
                "success",
            )
            return redirect(url_for("auth.login", portal="staff"))

    return render_template("register.html", form=form, staff=True)


@auth_bp.route("/login", methods=["GET", "POST"], defaults={"portal": "customer"})
@auth_bp.route("/login/<any(staff, admin):portal>", methods=["GET", "POST"])
def login(portal: str):
    if get_current_user() is not None:
        return redirect(url_for("account.index"))

    form = LoginForm()
    if form.validate_on_submit():
        ip = _client_ip()
        username_key = form.username.data.strip().lower()
        rate_key = f"{ip}:{username_key}"

        if login_limiter.is_limited(rate_key):
            record_event(
                AuditEvent.LOGIN_RATE_LIMITED,
                success=False,
                ip_address=ip,
                user_agent=_user_agent(),
                metadata={"username": username_key},
            )
            flash("Too many login attempts. Try again later.", "error")
            return render_template("login.html", form=form, portal=portal), 429

        user = authenticate_user(form.username.data, form.password.data)
        if user is None:
            login_limiter.record_failure(rate_key)
            record_event(
                AuditEvent.LOGIN_FAILURE,
                success=False,
                ip_address=ip,
                user_agent=_user_agent(),
                metadata={"username": username_key},
            )
            # Deliberately generic: does not say which of username/password
            # was wrong, and identical whether or not the username exists.
            flash("Invalid username or password.", "error")
        elif user.is_pending_staff or (portal in PORTAL_ROLES and user.role != PORTAL_ROLES[portal]):
            # Correct password, but no session is created. These messages are
            # only reachable with the right password, so they reveal nothing
            # to someone guessing usernames.
            pending = user.is_pending_staff
            login_limiter.reset(rate_key)
            record_event(
                AuditEvent.LOGIN_FAILURE,
                success=False,
                user_id=user.id,
                ip_address=ip,
                user_agent=_user_agent(),
                metadata={"reason": "staff_pending" if pending else f"not_{portal}"},
            )
            if pending:
                flash("Your staff account is waiting for administrator approval. "
                      "You can sign in once an admin approves it.", "error")
            else:
                flash(f"This account does not have {portal} access. "
                      "Please use the sign-in page for your account type.", "error")
        else:
            login_limiter.reset(rate_key)
            login_user(user)
            record_event(
                AuditEvent.LOGIN_SUCCESS,
                success=True,
                user_id=user.id,
                ip_address=ip,
                user_agent=_user_agent(),
            )
            return redirect(safe_next_target() or url_for("account.index"))

    return render_template("login.html", form=form, portal=portal)


@auth_bp.post("/logout")
@login_required
def logout():
    user = get_current_user()
    logout_user()
    record_event(
        AuditEvent.LOGOUT,
        success=True,
        user_id=user.id if user else None,
        ip_address=_client_ip(),
        user_agent=_user_agent(),
    )
    return redirect(url_for("auth.login"))
