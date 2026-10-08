"""Registration and authentication business logic.

No Flask request/session here -- routes parse the request and call these
functions with plain values, which is what keeps this module callable from
a script or a future admin tool, not just from HTTP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.user import Role, User
from app.services.audit import commit_with
from app.services.errors import ValidationError
from app.utils.security import (
    USERNAME_PATTERN,
    hash_password,
    needs_rehash,
    normalize_email,
    normalize_username,
    password_policy_errors,
    verify_password,
)

logger = logging.getLogger(__name__)

# A real Argon2id hash of an unguessable, unused password. Verifying against
# this when no account matches keeps the failure path doing the same
# constant-ish amount of work as a real mismatch, instead of returning
# early and letting a timing difference confirm "that username doesn't
# exist" to an attacker.
_DUMMY_HASH = hash_password("correct horse battery staple not a real password 39f2")


class RegistrationError(ValidationError):
    pass


@dataclass
class RegistrationInput:
    username: str
    email: str
    password: str
    password_confirm: str


def _password_errors(data: RegistrationInput) -> dict[str, list[str]]:
    """Password policy and confirmation, shared by every way a password is set."""
    errors: dict[str, list[str]] = {}
    if password_errors := password_policy_errors(data.password):
        errors["password"] = password_errors
    if data.password != data.password_confirm:
        errors.setdefault("password_confirm", []).append("Passwords do not match.")
    return errors


def _duplicate_errors(username: str, email: str) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    # Pre-check for a friendly, field-specific message. The database's
    # unique constraint is the real backstop against the race between this
    # check and the insert that follows.
    if db.session.query(User.id).filter_by(username=username).first() is not None:
        errors.setdefault("username", []).append("That username is already taken.")
    if db.session.query(User.id).filter_by(email=email).first() is not None:
        errors.setdefault("email", []).append("That email is already registered.")
    return errors


def _account_errors(data: RegistrationInput, username: str, email: str) -> dict[str, list[str]]:
    """Password policy, confirmation and duplicate checks shared by every way
    an account is created. ``username``/``email`` are already normalized."""
    errors = _password_errors(data)
    for field, messages in _duplicate_errors(username, email).items():
        errors.setdefault(field, []).extend(messages)
    return errors


def identity_errors(raw_username: str | None, raw_email: str | None) -> tuple[str, str, dict[str, list[str]]]:
    """Username pattern, email syntax and duplicates for an account created
    without the web registration form in front of it (``flask create-admin``,
    staff invitations). Returns the normalized username and email plus any
    errors. Email syntax uses the same ``email_validator`` package as the
    registration form's ``Email()`` validator."""
    from email_validator import EmailNotValidError, validate_email

    errors: dict[str, list[str]] = {}
    if not USERNAME_PATTERN.fullmatch((raw_username or "").strip()):
        errors["username"] = ["Username must be 3-32 characters: letters, digits or underscores."]
    raw_email = (raw_email or "").strip()
    try:
        if len(raw_email) > 255:
            raise EmailNotValidError("too long")
        validate_email(raw_email, check_deliverability=False)
    except EmailNotValidError:
        errors["email"] = ["Enter a valid email address."]
    username, email = normalize_username(raw_username or ""), normalize_email(raw_email)
    for field, messages in _duplicate_errors(username, email).items():
        errors.setdefault(field, []).extend(messages)
    return username, email, errors


def register_user(data: RegistrationInput, *, audit=None) -> User:
    """Create a CUSTOMER account -- the only role public registration can
    create. STAFF accounts come only from an admin's invitation
    (app/services/accounts.py, M4); ADMIN accounts only from ``create_admin``,
    the CLI path.

    ``audit(user)`` adds the registration's audit row in the same transaction
    as the account (app/services/audit.py::commit_with)."""
    username = normalize_username(data.username)
    email = normalize_email(data.email)

    if errors := _account_errors(data, username, email):
        raise RegistrationError(errors)

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(data.password),
        role=Role.CUSTOMER,  # never a client-supplied role
        staff_approved=False,
    )
    db.session.add(user)
    try:
        commit_with(audit, user)
    except IntegrityError:
        # Lost the pre-check race against a concurrent registration.
        raise RegistrationError({"_general": ["That username or email is already registered."]})

    return user


def create_admin(data: RegistrationInput) -> User:
    """Create an ADMIN account -- the ``flask create-admin`` path, never HTTP.

    Same password policy, confirmation, normalization, duplicate checks and
    Argon2id hashing as ``register_user``. Because no web form sits in front
    of this function, it also applies the form-layer rules itself: the
    username pattern and email syntax (the same ``email_validator`` package
    the registration form's ``Email()`` uses).

    The account and its ``admin_created`` audit row are written in **one
    transaction**: either both commit or neither does, so a failure can leave
    neither a half-created admin nor an orphaned audit row.
    """
    # Local imports: audit sits beside this module in the service layer.
    from app.models.audit_log import AuditEvent
    from app.services.audit import mirror_to_security_log, record_event

    username, email, errors = identity_errors(data.username, data.email)
    for field, messages in _password_errors(data).items():
        errors.setdefault(field, []).extend(messages)
    if errors:
        raise RegistrationError(errors)

    user = User(username=username, email=email, password_hash=hash_password(data.password),
                role=Role.ADMIN, staff_approved=False)
    try:
        db.session.add(user)
        db.session.flush()  # assigns user.id for the audit row
        entry = record_event(AuditEvent.ADMIN_CREATED, success=True, user_id=user.id,
                             metadata={"source": "cli"}, commit=False)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Lost the pre-check race against a concurrent registration.
        raise RegistrationError({"_general": ["That username or email is already registered."]})
    except Exception:
        db.session.rollback()
        raise
    mirror_to_security_log(entry)
    return user


def authenticate_user(username: str, password: str) -> User | None:
    """Return the User on success, or None on any failure.

    Every failure reason -- unknown username, wrong password, inactive
    account -- returns the identical None, so a caller cannot distinguish
    them and neither can the response built from it.
    """
    user = db.session.query(User).filter_by(username=normalize_username(username)).first()

    if user is None:
        verify_password(_DUMMY_HASH, password)  # timing parity, see _DUMMY_HASH
        return None

    if not verify_password(user.password_hash, password):
        return None

    if not user.is_active:
        return None

    # Only reachable with the correct password, so the plaintext in hand is
    # verified: re-hash it if the stored hash used older Argon2 parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        try:
            db.session.commit()
        except SQLAlchemyError:
            # The old hash still verifies; the upgrade is retried next login.
            db.session.rollback()
            logger.warning("Could not store re-hashed password for user_id=%s", user.id)

    return user
