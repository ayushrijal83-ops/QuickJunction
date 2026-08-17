"""Password hashing and server-side password/identifier policy.

Argon2id (via argon2-cffi) with library defaults, which already target the
OWASP-recommended parameters and self-upgrade as the library's defaults
improve. Rolling a custom parameter set is how projects end up under-salted
or under-iterated; the maintained default is the lazy and the correct
choice here.
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

# A handful of the most common breached passwords. Deliberately small: a
# real deny-list is a maintained data feed (e.g. HaveIBeenPwned's k-anonymity
# API), not a hand-typed list, and adding one is future work, not this
# milestone's.
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789",
    "qwerty123", "letmein123", "welcome123", "admin1234", "iloveyou1",
}

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128  # bounds the work argon2 does on attacker input

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{3,32}$")


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    try:
        return _hasher.verify(password_hash, plain)
    except VerifyMismatchError:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_policy_errors(password: str) -> list[str]:
    """Return human-readable policy violations, empty if the password passes."""
    errors = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long.")
    if len(password) > PASSWORD_MAX_LENGTH:
        errors.append(f"Password must be at most {PASSWORD_MAX_LENGTH} characters long.")
    if not re.search(r"[A-Za-z]", password):
        errors.append("Password must contain at least one letter.")
    if not re.search(r"[0-9]", password):
        errors.append("Password must contain at least one digit.")
    if password.lower() in _COMMON_PASSWORDS:
        errors.append("Password is too common. Choose a less predictable password.")
    return errors


def normalize_email(email: str) -> str:
    """Trim and lowercase for storage/lookup. Not full RFC normalization --
    WTForms' Email validator has already confirmed the shape."""
    return email.strip().lower()


def normalize_username(username: str) -> str:
    """Lowercase for storage/lookup, so uniqueness does not depend on the
    database's collation (MySQL's default is case-insensitive, SQLite's is
    not -- normalizing here keeps both backends behaving the same way)."""
    return username.strip().lower()
