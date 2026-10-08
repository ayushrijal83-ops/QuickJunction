"""Account security (M4): password change, admin-issued password resets,
staff invitations, and account activation.

**One-time codes.** Invitations and resets share one mechanism
(``AccountToken``): 256 random bits from ``secrets``, shown once to the
issuing admin, stored only as a SHA-256 digest, expiring after a configured
number of hours, and redeemable exactly once. Redemption is a conditional
UPDATE (``used_at IS NULL AND expires_at > now``), so two simultaneous
redemptions of one code cannot both succeed. Codes are entered into a POST
form at ``/account/setup`` -- never placed in a URL -- so they cannot end up
in access logs, browser history or a Referer header.

**Admins never choose or see another user's password.** A reset issues a
code; the account holder sets the new password with it.

**Revocation.** Every event that should end existing sessions increments
``User.session_version`` (the M09 mechanism ``get_current_user`` already
checks on every request): a password change (the caller re-stamps the
current session), issuing or completing a reset, accepting an invitation,
and deactivation. Deactivation also voids the account's open codes, so
reactivating an account never revives an old cookie or an old code.

Every change and its audit row commit together (``commit_with``, M2).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import sqlalchemy as sa
from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.account_token import AccountToken, TokenPurpose
from app.models.user import Role, User
from app.services.audit import commit_with
from app.services.auth import RegistrationInput, _password_errors, identity_errors
from app.services.errors import ValidationError
from app.utils.clock import db_now
from app.utils.security import PASSWORD_MAX_LENGTH, hash_password, verify_password


class AccountError(ValidationError):
    pass


class CodeRejected(AccountError):
    """An unknown, expired, already-used or voided code. Deliberately one
    message for every reason. ``purpose`` is set only when the code matched a
    row, for the audit record -- never shown to the caller."""

    def __init__(self, purpose: TokenPurpose | None = None):
        super().__init__({"code": ["That code is invalid or has expired. Ask an administrator for a new one."]})
        self.purpose = purpose


_CODE_BYTES = 32  # 256 bits
_MAX_CODE_CHARS = 128


def _digest(raw_code: str) -> str:
    return hashlib.sha256(raw_code.strip().encode("utf-8")).hexdigest()


def _require_admin(actor: User) -> None:
    if actor.role != Role.ADMIN:
        raise AccountError({"account": ["Only an administrator can do that."]})


def _void_open_codes(user_id: int, purpose: TokenPurpose | None = None) -> None:
    stmt = (sa.update(AccountToken)
            .where(AccountToken.user_id == user_id, AccountToken.used_at.is_(None))
            .values(used_at=sa.func.now())
            .execution_options(synchronize_session=False))
    if purpose is not None:
        stmt = stmt.where(AccountToken.purpose == purpose)
    db.session.execute(stmt)


def _issue_code(user: User, purpose: TokenPurpose, actor: User, ttl_hours: int) -> str:
    """Add a new code row (superseding any open one of the same purpose) to
    the current transaction and return the raw code. The caller commits."""
    _void_open_codes(user.id, purpose)
    raw = secrets.token_urlsafe(_CODE_BYTES)
    db.session.add(AccountToken(user_id=user.id, purpose=purpose, token_hash=_digest(raw), created_by_id=actor.id,
                                expires_at=db_now() + timedelta(hours=ttl_hours)))
    return raw


# --- self-service password change ---------------------------------------------------


def change_password(user: User, current: str, new: str, confirm: str, *, audit=None) -> None:
    """Verify the current password, apply the password policy, store a new
    Argon2id hash and revoke every session (the route re-stamps its own)."""
    if not verify_password(user.password_hash, (current or "")[:PASSWORD_MAX_LENGTH]):
        raise AccountError({"current_password": ["Your current password is incorrect."]})
    errors = _password_errors(RegistrationInput("", "", new or "", confirm or ""))
    if not errors and new == current:
        errors["password"] = ["Choose a password you are not already using."]
    if errors:
        raise AccountError(errors)
    user.password_hash = hash_password(new)
    user.session_version = (user.session_version or 0) + 1
    commit_with(audit, user)


# --- staff invitations ------------------------------------------------------------------


def invite_staff(actor: User, raw_username: str | None, raw_email: str | None, *, audit=None) -> tuple[User, str]:
    """Create a STAFF account that cannot be used until its invitation code is
    redeemed, and return it with the raw code.

    The account is inactive and its password hash is of 256 random bits that
    are immediately discarded, so nobody -- the admin included -- knows a
    password for it. The role is fixed here; nothing in the request names one.
    """
    _require_admin(actor)
    username, email, errors = identity_errors(raw_username, raw_email)
    if errors:
        raise AccountError(errors)
    user = User(username=username, email=email, password_hash=hash_password(secrets.token_urlsafe(_CODE_BYTES)),
                role=Role.STAFF, staff_approved=True, is_active=False)
    try:
        db.session.add(user)
        db.session.flush()
        code = _issue_code(user, TokenPurpose.STAFF_INVITE, actor, current_app.config["STAFF_INVITE_TTL_HOURS"])
        commit_with(audit, user)
    except IntegrityError:
        db.session.rollback()
        raise AccountError({"_general": ["That username or email is already registered."]})
    return user, code


# --- admin-issued password reset --------------------------------------------------------


def issue_password_reset(actor: User, target: User, *, audit=None) -> str:
    """Issue a reset code for ``target`` and end its sessions now: a reset is
    often prompted by a suspected compromise. The old password keeps working
    until the code is redeemed (deactivate the account to lock it out)."""
    _require_admin(actor)
    if target.id == actor.id:
        raise AccountError({"account": ["Use Change password for your own account."]})
    if not target.is_active:
        raise AccountError({"account": [f"{target.username} is deactivated. Activate the account first."]})
    target.session_version = (target.session_version or 0) + 1
    code = _issue_code(target, TokenPurpose.PASSWORD_RESET, actor, current_app.config["PASSWORD_RESET_TTL_HOURS"])
    commit_with(audit, target)
    return code


# --- redeeming a code ----------------------------------------------------------------------


def redeem_code(raw_code: str | None, password: str, confirm: str, *, audit=None) -> tuple[User, TokenPurpose]:
    """Set the account's password from a valid code. For an invitation this
    also activates the account. Revokes every existing session and voids the
    account's other open codes. The password is validated first, so a typo
    in it does not burn the code."""
    errors = _password_errors(RegistrationInput("", "", password or "", confirm or ""))
    if errors:
        raise AccountError(errors)
    raw_code = (raw_code or "").strip()
    if not raw_code or len(raw_code) > _MAX_CODE_CHARS:
        raise CodeRejected()

    token = db.session.query(AccountToken).filter_by(token_hash=_digest(raw_code)).first()
    if token is None:
        raise CodeRejected()
    purpose, now = token.purpose, db_now()
    if token.used_at is not None or token.expires_at <= now:
        raise CodeRejected(purpose)

    new_hash = hash_password(password)  # before taking the row lock below
    # The single-use guard: only one transaction can move used_at off NULL.
    won = db.session.execute(
        sa.update(AccountToken)
        .where(AccountToken.id == token.id, AccountToken.used_at.is_(None), AccountToken.expires_at > now)
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    user = db.session.get(User, token.user_id)
    if not won or (purpose == TokenPurpose.PASSWORD_RESET and not user.is_active):
        db.session.rollback()
        raise CodeRejected(purpose)

    user.password_hash = new_hash
    user.session_version = (user.session_version or 0) + 1
    if purpose == TokenPurpose.STAFF_INVITE:
        user.is_active, user.staff_approved = True, True
    _void_open_codes(user.id)
    commit_with(audit, user, purpose)
    return user, purpose


# --- activation ---------------------------------------------------------------------------


def _ensure_another_admin_stays(target: User) -> None:
    """Refuse to deactivate the last active admin.

    Locks every admin row (``SELECT ... FOR UPDATE``, in id order so two
    callers cannot deadlock) and counts the active ones from that locked,
    freshly read state: two admins deactivating each other at once are
    serialised, and the second sees the first's change and is refused.
    An admin created concurrently can only raise the count."""
    admin_ids = db.session.scalars(sa.select(User.id).where(User.role == Role.ADMIN).order_by(User.id)).all()
    admins = db.session.scalars(
        sa.select(User).where(User.id.in_(admin_ids)).order_by(User.id).with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if not any(a.is_active and a.id != target.id for a in admins):
        db.session.rollback()
        raise AccountError({"account": ["At least one other active administrator must remain."]})


def set_active(actor: User, target: User, active: bool, *, audit=None) -> bool:
    """Activate or deactivate ``target``. Returns False if nothing changed.

    Deactivation ends every session now (``session_version``) and voids open
    codes. An admin cannot deactivate themselves, nor the last active admin."""
    _require_admin(actor)
    if not active and target.id == actor.id:
        raise AccountError({"account": ["You cannot deactivate your own account."]})
    if not active and target.role == Role.ADMIN:
        _ensure_another_admin_stays(target)
    if target.is_active == active:
        db.session.rollback()
        return False
    target.is_active = active
    if not active:
        target.session_version = (target.session_version or 0) + 1
        _void_open_codes(target.id)
    commit_with(audit, target)
    return True


# --- listing -----------------------------------------------------------------------------


def list_accounts(role: Role | None = None) -> list[User]:
    query = db.session.query(User)
    if role is not None:
        query = query.filter(User.role == role)
    return query.order_by(User.role, User.username).all()


def open_invitation_user_ids() -> set[int]:
    """Accounts with an unused, unexpired invitation -- shown as "Invited"."""
    return set(db.session.scalars(sa.select(AccountToken.user_id).where(
        AccountToken.purpose == TokenPurpose.STAFF_INVITE, AccountToken.used_at.is_(None),
        AccountToken.expires_at > db_now())))
