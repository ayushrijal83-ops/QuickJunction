"""One-time account codes (M4): staff invitations and admin-issued password
resets.

The raw code is shown once to the admin who issued it and never stored:
``token_hash`` is its SHA-256 hex digest. A plain (unsalted, unkeyed) hash is
enough because the code is 256 random bits -- there is no dictionary to
precompute -- and it lets redemption find the row with one indexed lookup.

A code is usable while ``used_at`` is NULL and ``expires_at`` is in the
future. Redeeming, superseding (a newer code of the same purpose for the same
account) and deactivating the account all set ``used_at``, so a code works at
most once and dies with the account's access.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import enum_column


class TokenPurpose(str, enum.Enum):
    STAFF_INVITE = "staff_invite"
    PASSWORD_RESET = "password_reset"


class AccountToken(db.Model):
    __tablename__ = "account_tokens"
    __table_args__ = (sa.Index("ix_account_tokens_user_purpose", "user_id", "purpose"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    purpose: Mapped[TokenPurpose] = mapped_column(
        enum_column(TokenPurpose, 16, name="ck_account_tokens_purpose"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    created_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<AccountToken id={self.id} user_id={self.user_id} purpose={self.purpose.value}>"
