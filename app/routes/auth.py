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
from app.services.audit import login_identifier, record_event
from app.services.auth import RegistrationError, RegistrationInput, authenticate_user, register_user
from app.utils.authorization import get_current_user, login_required, login_user, logout_user
from app.utils.errors import with_retry_after
from app.utils.ratelimit import login_limiter, registration_limiter
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


def _registration_audit(event: AuditEvent):
    """Audit callback for register_user: the row commits with the account."""
    return lambda user: record_event(event, success=True, user_id=user.id, ip_address=_client_ip(),
                                     user_agent=_user_agent())


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user() is not None:
        return redirect(url_for("account.index"))

    form = RegistrationForm()
    if request.method == "POST":
        # Every submission counts, valid or not, keyed by IP alone: rotating
        # usernames or emails neither resets it nor targets anyone else.
        decision = registration_limiter.attempt(_client_ip() or "unknown")
        if not decision.allowed:
            flash("Too many sign-up attempts. Try again later.", "error")
            return with_retry_after(render_template("register.html", form=form, staff=False), decision)
    if form.validate_on_submit():
        try:
            user = register_user(
                RegistrationInput(
                    username=form.username.data,
                    email=form.email.data,
                    password=form.password.data,
                    password_confirm=form.password_confirm.data,
                ),
                audit=_registration_audit(AuditEvent.REGISTER_SUCCESS),
            )
        except RegistrationError as exc:
            for field_name, messages in exc.errors.items():
                if hasattr(form, field_name):
                    getattr(form, field_name).errors.extend(messages)
                else:
                    for message in messages:
                        flash(message, "error")
        else:
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

        # Check and count in one atomic step: this attempt is recorded as a
        # failure *before* the password is checked, so concurrent attempts
        # each hold a slot and cannot together exceed the limit (across
        # workers while Redis is up; per worker during an outage). A wrong
        # password leaves the reservation as the recorded failure; a right one
        # resets the key (as it always has); no verdict releases it.
        reservation = login_limiter.try_reserve(rate_key)
        if not reservation.allowed:
            record_event(
                AuditEvent.LOGIN_RATE_LIMITED,
                success=False,
                ip_address=ip,
                user_agent=_user_agent(),
                metadata={"login_id": login_identifier(username_key)},
                commit=True,
            )
            flash("Too many login attempts. Try again later.", "error")
            return with_retry_after(render_template("login.html", form=form, portal=portal), reservation)

        try:
            user = authenticate_user(form.username.data, form.password.data)
        except BaseException:
            login_limiter.release(rate_key, reservation.token)  # no verdict: not a failure
            raise
        if user is None:
            # The reservation made above is this failure; nothing to record.
            record_event(
                AuditEvent.LOGIN_FAILURE,
                success=False,
                ip_address=ip,
                user_agent=_user_agent(),
                # Never the typed username: see login_identifier.
                metadata={"login_id": login_identifier(username_key)},
                commit=True,
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
                commit=True,
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
                commit=True,
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
        commit=True,  # independent: the revocation above must stand on its own
    )
    return redirect(url_for("auth.login"))
