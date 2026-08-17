"""Registration and authentication business logic.

No Flask request/session here -- routes parse the request and call these
functions with plain values, which is what keeps this module callable from
a script or a future admin tool, not just from HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.user import Role, User
from app.services.errors import ValidationError
from app.utils.security import (
    hash_password,
    normalize_email,
    normalize_username,
    password_policy_errors,
    verify_password,
)

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


def register_user(data: RegistrationInput) -> User:
    username = normalize_username(data.username)
    email = normalize_email(data.email)

    errors: dict[str, list[str]] = {}

    if password_errors := password_policy_errors(data.password):
        errors["password"] = password_errors
    if data.password != data.password_confirm:
        errors.setdefault("password_confirm", []).append("Passwords do not match.")

    # Pre-check for a friendly, field-specific message. The database's
    # unique constraint is the real backstop against the race between this
    # check and the insert below.
    if db.session.query(User.id).filter_by(username=username).first() is not None:
        errors.setdefault("username", []).append("That username is already taken.")
    if db.session.query(User.id).filter_by(email=email).first() is not None:
        errors.setdefault("email", []).append("That email is already registered.")

    if errors:
        raise RegistrationError(errors)

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(data.password),
        role=Role.CUSTOMER,  # never trust a client-supplied role
    )
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Lost the pre-check race against a concurrent registration.
        raise RegistrationError({"_general": ["That username or email is already registered."]})

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

    return user
