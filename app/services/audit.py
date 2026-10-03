"""Append-only writer for the security audit trail.

The only place a row is allowed to reach ``audit_logs``. Callers pass plain
values, not a Flask request, so the same call works from a route, a script,
or a test -- see app/services/__init__.py.
"""

from __future__ import annotations

import json

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
    commit: bool = True,
) -> AuditLog:
    """Write one audit row.

    ``commit=False`` only adds the row to the caller's session, so it commits
    or rolls back together with the change it describes; the caller then
    owns the commit and the security-log mirror (see
    ``app/services/auth.create_admin``).
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
