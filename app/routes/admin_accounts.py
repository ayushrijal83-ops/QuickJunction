"""Admin account management (M4): list accounts, invite staff, issue
password-reset codes, activate and deactivate.

ADMIN only (``require_role``), POST + CSRF for every change, and the
services re-check the actor's role. The subject account comes from the URL's
``<int:...>`` converter and is re-fetched; no route reads a role, status or
user id from the form, so there is nothing to escalate with.

A one-time code is rendered once, directly in the POST response: never
flashed (the flash store is the signed-but-readable session cookie), never
redirected through a URL, never logged. Signed-in pages are ``no-store``
(app/utils/headers.py), so the browser does not cache it either.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models.audit_log import AuditEvent
from app.models.user import Role, User
from app.routes.forms import InviteStaffForm
from app.services.accounts import (
    AccountError,
    invite_staff,
    issue_password_reset,
    list_accounts,
    open_invitation_user_ids,
    set_active,
)
from app.services.audit import record_event
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

admin_accounts_bp = Blueprint("admin_accounts", __name__, url_prefix="/admin/accounts")


def _audit(event: AuditEvent, target: User, **extra):
    """Business-event audit row, added inside the service's transaction."""
    return record_event(event, success=True, user_id=get_current_user().id, ip_address=client_ip(),
                        user_agent=user_agent(), metadata={"target_user_id": target.id, **extra})


def _target(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    return user


def _flash_errors(exc: AccountError) -> None:
    for messages in exc.errors.values():
        for message in messages:
            flash(message, "error")


@admin_accounts_bp.get("")
@require_role(Role.ADMIN)
def account_list():
    raw_role = request.args.get("role", "")
    role = Role(raw_role) if raw_role in {r.value for r in Role} else None
    return render_template("admin/accounts.html", accounts=list_accounts(role), role=role, roles=list(Role),
                           invited=open_invitation_user_ids(), me=get_current_user())


@admin_accounts_bp.route("/invite", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def invite():
    form = InviteStaffForm()
    if form.validate_on_submit():
        try:
            user, code = invite_staff(get_current_user(), form.username.data, form.email.data,
                                      audit=lambda u: _audit(AuditEvent.STAFF_INVITED, u))
        except AccountError as exc:
            for field_name, messages in exc.errors.items():
                target = getattr(form, field_name, None)
                if target is not None and hasattr(target, "errors"):
                    target.errors = list(target.errors) + messages
                else:
                    for message in messages:
                        flash(message, "error")
        else:
            return render_template("admin/account_code.html", user=user, code=code, purpose="invite")
    return render_template("admin/account_invite.html", form=form)


@admin_accounts_bp.post("/<int:user_id>/reset")
@require_role(Role.ADMIN)
def reset(user_id: int):
    target = _target(user_id)  # CSRF: app-wide CSRFProtect, as for admin_staff
    try:
        code = issue_password_reset(get_current_user(), target,
                                    audit=lambda t: _audit(AuditEvent.PASSWORD_RESET_ISSUED, t))
    except AccountError as exc:
        _flash_errors(exc)
        return redirect(url_for("admin_accounts.account_list"))
    return render_template("admin/account_code.html", user=target, code=code, purpose="reset")


def _set_active(user_id: int, active: bool):
    target = _target(user_id)  # CSRF: app-wide CSRFProtect, as for admin_staff
    event = AuditEvent.ACCOUNT_ACTIVATED if active else AuditEvent.ACCOUNT_DEACTIVATED
    try:
        changed = set_active(get_current_user(), target, active,
                             audit=lambda t: _audit(event, t, role=t.role.value))
    except AccountError as exc:
        _flash_errors(exc)
    else:
        if changed:
            flash(f"{target.username} is now {'active' if active else 'deactivated'}.", "success")
    return redirect(url_for("admin_accounts.account_list"))


@admin_accounts_bp.post("/<int:user_id>/deactivate")
@require_role(Role.ADMIN)
def deactivate(user_id: int):
    return _set_active(user_id, False)


@admin_accounts_bp.post("/<int:user_id>/activate")
@require_role(Role.ADMIN)
def activate(user_id: int):
    return _set_active(user_id, True)
