"""Security audit trail.

Append-only, and deliberately narrow: an identifier, an outcome, and enough
network/context detail to investigate abuse. No credential, token, or
request body is ever eligible for a column here -- see
``app/services/audit.py``, the only writer.

Retention: not enforced in code yet. Treat these rows as sensitive
(source IPs + login activity) with a defined retention window (90 days is a
reasonable starting point) and access restricted to operators -- see
docs/SECURITY.md.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import enum_column


class AuditEvent(str, enum.Enum):
    REGISTER_SUCCESS = "register_success"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGIN_RATE_LIMITED = "login_rate_limited"
    LOGOUT = "logout"

    # Admin menu-management actions. user_id is the acting admin, not a
    # subject account; the affected category/menu item id lives in
    # metadata_json (see app/services/menu.py).
    CATEGORY_CREATED = "category_created"
    CATEGORY_UPDATED = "category_updated"
    CATEGORY_DEACTIVATED = "category_deactivated"
    MENU_ITEM_CREATED = "menu_item_created"
    MENU_ITEM_UPDATED = "menu_item_updated"
    MENU_ITEM_AVAILABILITY_CHANGED = "menu_item_availability_changed"
    INGREDIENT_CHANGED = "ingredient_changed"

    # Checkout. user_id is the customer placing the order; the order id and
    # total live in metadata_json (see app/services/orders.py).
    ORDER_CREATED = "order_created"
    ORDER_CREATION_FAILED = "order_creation_failed"

    # Order status changes. user_id is the acting account -- staff/admin for
    # the kitchen workflow, or the customer for a self-service cancellation;
    # metadata_json carries the order id, from/to statuses and an "actor"
    # role (see app/routes/staff_orders.py, app/routes/orders.py).
    ORDER_STATUS_CHANGED = "order_status_changed"
    ORDER_STATUS_CHANGE_REJECTED = "order_status_change_rejected"

    # Staff approval. STAFF_REGISTERED's user_id is the new (pending) account;
    # for approve/revoke user_id is the acting admin and the subject staff id
    # lives in metadata_json (see app/routes/admin_staff.py).
    STAFF_REGISTERED = "staff_registered"
    STAFF_APPROVED = "staff_approved"
    STAFF_APPROVAL_REVOKED = "staff_approval_revoked"

    # Restaurant tables. user_id is the acting staff/admin member; the table
    # id (and from/to status) live in metadata_json (see app/routes/tables.py).
    TABLE_CREATED = "table_created"
    TABLE_UPDATED = "table_updated"
    TABLE_STATUS_CHANGED = "table_status_changed"

    # Reservations. user_id is the acting account (the booking customer, or
    # the staff/admin member changing its status); reservation/table ids,
    # from/to status and the actor's role live in metadata_json.
    RESERVATION_CREATED = "reservation_created"
    RESERVATION_STATUS_CHANGED = "reservation_status_changed"


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    event_type: Mapped[AuditEvent] = mapped_column(
        enum_column(AuditEvent, 32, name="ck_audit_logs_event_type"), nullable=False, index=True
    )

    # The account the event is about (auth events) or the acting admin
    # (menu-management events). Null when the event names no account, e.g.
    # a failed login for a username that does not exist. ondelete=SET NULL
    # keeps the audit row (and its metadata) after an account is removed.
    user_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    success: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)

    ip_address: Mapped[str | None] = mapped_column(sa.String(45), nullable=True)  # IPv6 max length
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    # Small, non-sensitive context only, e.g. {"username": "alice"} on a
    # failed login. Never a password, hash, token, or full request body.
    metadata_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<AuditLog id={self.id} event={self.event_type.value} success={self.success}>"
