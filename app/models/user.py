"""User account and role.

Role is a constrained string, not a table: three fixed roles with no
per-role attributes do not earn a join. ``enum_column`` (see
app/models/enums.py) stores it as ``VARCHAR`` with a ``CHECK`` constraint,
so adding a role later is a plain column-constraint migration instead of an
``ALTER TYPE``.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import enum_column


class Role(str, enum.Enum):
    ADMIN = "admin"
    STAFF = "staff"
    CUSTOMER = "customer"


class User(db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Case-sensitive as stored; uniqueness is compared on the lowercased
    # value the application always writes (see app/utils/security.py).
    username: Mapped[str] = mapped_column(sa.String(32), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(sa.String(255), unique=True, nullable=False)

    # Argon2id encoded hash (~100 chars); 255 leaves headroom for parameter
    # changes without a migration.
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    role: Mapped[Role] = mapped_column(
        enum_column(Role, 16, name="ck_users_role"),
        nullable=False,
        server_default=Role.CUSTOMER.value,
    )

    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<User id={self.id} username={self.username!r} role={self.role.value}>"
