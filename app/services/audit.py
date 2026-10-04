"""Append-only writer for the security audit trail.

The only place a row is allowed to reach ``audit_logs``. Callers pass plain
values, not a Flask request, so the same call works from a route, a script,
or a test -- see app/services/__init__.py.

**Transactions (M2).** ``record_event`` only *adds* the row to the caller's
session; it does not commit unless asked to. Two patterns:

* **Business event** -- the audit row describes a change. The service that
  makes the change takes an ``audit`` callback and commits through
  ``commit_with``, which adds the row inside the same transaction: both
  commit or neither does. A failed audit write rolls the change back.
* **Independent event** -- nothing else is being written, or the event must
  survive regardless (login success/failure, rate limiting, logout, rejected
  attempts). The caller passes ``commit=True``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Callable

from flask import current_app

from app.extensions import db
from app.models.audit_log import AuditEvent, AuditLog
from app.utils.logging import get_security_logger

_FORBIDDEN_METADATA_KEYS = {"password", "password_hash", "token", "session", "secret", "cookie"}

_MAX_METADATA_CHARS = 500
_MAX_USER_AGENT_CHARS = 255


def record_event(
    event_type: AuditEvent,
    *,
    success: bool,
    user_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
    commit: bool = False,
) -> AuditLog:
    """Add one audit row to the current transaction.

    By default the row is only added to the session, so it commits or rolls
    back with the change it describes; the caller owns the commit (usually
    via ``commit_with``) and the security-log mirror. ``commit=True`` is for
    independent events only -- it commits the session immediately.
    """
    if metadata:
        leaked = _FORBIDDEN_METADATA_KEYS & metadata.keys()
        if leaked:
            raise ValueError(f"Refusing to audit-log sensitive keys: {sorted(leaked)}")

    metadata_json = json.dumps(metadata)[:_MAX_METADATA_CHARS] if metadata else None

    entry = AuditLog(
        event_type=event_type,
        success=success,
        user_id=user_id,
        ip_address=(ip_address or None)[:45] if ip_address else None,
        user_agent=(user_agent or None)[:_MAX_USER_AGENT_CHARS] if user_agent else None,
        metadata_json=metadata_json,
    )
    db.session.add(entry)
    if not commit:
        return entry
    db.session.commit()
    mirror_to_security_log(entry)
    return entry


def mirror_to_security_log(entry: AuditLog) -> None:
    """Mirrored to a log file, not just the database: a compromised or
    unavailable database should not be the only way to lose visibility into
    auth activity. Call only after the row has committed."""
    level = "info" if entry.success else "warning"
    getattr(get_security_logger(), level)(
        "%s user_id=%s ip=%s", entry.event_type.value, entry.user_id, entry.ip_address
    )


def commit_with(audit: Callable[..., AuditLog | None] | None = None, *args) -> None:
    """Commit the current transaction with its audit row in it.

    ``audit(*args)`` runs first and adds the row (``record_event`` with the
    default ``commit=False``); then one commit covers the business change and
    the row. Any failure in either rolls back both and re-raises, so a change
    can never commit without its audit event. The row is mirrored to the
    security log only once it has committed.
    """
    try:
        entry = None
        if audit is not None:
            db.session.flush()  # new rows get their ids before the audit row names them
            entry = audit(*args)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if isinstance(entry, AuditLog):
        mirror_to_security_log(entry)


def login_identifier(raw_username: str | None) -> str:
    """What a failed-login audit row stores instead of the typed username.

    The login field is attacker-controlled: it can be anything up to the form
    limit, including a password typed into the wrong box. So it is never
    stored. Instead: HMAC-SHA256 of the normalized (trimmed, lowercased) input,
    keyed with ``SECRET_KEY``, truncated to 32 hex characters. Deterministic,
    so repeated attempts against one name share an identifier; keyed, so the
    short, guessable space of usernames cannot be reversed with a lookup table
    by someone who can read the audit table but not the server's secret. An
    investigator with the secret can check a specific name with the same
    function. Rotating ``SECRET_KEY`` changes every identifier.
    """
    normalized = (raw_username or "").strip().lower()
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, normalized.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:32]
