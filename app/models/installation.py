"""Local organization, branch and installation identity (O1).

Each branch runs its own installation with its own database. These three
tables give that installation a persistent local identity that never needs the
internet:

- ``organizations``: the business that owns the branch.
- ``branches``: this branch, belonging to one organization.
- ``installation``: one row (id = 1, ``ck_installation_singleton``, the same
  pattern as ``restaurant_profile``) naming the branch this database serves.

Every ``public_id`` is a random UUID generated locally, exactly once, when
the row is first inserted (migration 4c7e2a9f1b38 creates the rows for an
existing or fresh database). Nothing generates or replaces one on startup
or on read -- ``app/services/installation.py::get_installation`` only reads.

``public_id`` protection is partial and ORM-level only. The ``@validates``
hooks below block direct attribute assignment (``obj.public_id = ...``) and
``session.merge()`` of a copy carrying a different ``public_id``. They do
NOT see bulk ORM updates -- ``Query.update()``,
``session.execute(sa.update(...))`` and ``bulk_update_mappings()`` -- or raw
SQL, all of which can change a ``public_id``. The database does not enforce
immutability.

``hq_id`` is reserved for an identifier issued later by a future HQ service
when the branch is linked; it stays NULL until then. Its format is not yet
decided, so it is an opaque string.

Deliberately not tenancy: no operational table (orders, payments,
inventory, reservations, customers, audit logs) references these tables or
carries a branch id -- the whole database already belongs to one branch.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.extensions import db

INSTALLATION_ID = 1
UUID_LENGTH = 36  # canonical text form, e.g. 1b4e28ba-2fa1-11d2-883f-0016d3cca427
NAME_MAX = 120  # same as restaurant_profile.name, which seeds both names
HQ_ID_MAX = 64


def new_public_id() -> str:
    return str(uuid.uuid4())


def _keep_once_set(instance, key: str, value):
    current = getattr(instance, key)
    if current is not None and value != current:
        raise ValueError(f"{type(instance).__name__}.{key} is permanent and cannot be changed.")
    return value


class Organization(db.Model):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        sa.String(UUID_LENGTH), unique=True, nullable=False, default=new_public_id
    )
    name: Mapped[str] = mapped_column(sa.String(NAME_MAX), nullable=False)
    hq_id: Mapped[str | None] = mapped_column(sa.String(HQ_ID_MAX), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)

    branches: Mapped[list["Branch"]] = relationship(back_populates="organization", passive_deletes="all")

    @validates("public_id")
    def _public_id_is_permanent(self, key, value):
        return _keep_once_set(self, key, value)

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Organization {self.public_id} name={self.name!r}>"


class Branch(db.Model):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    public_id: Mapped[str] = mapped_column(
        sa.String(UUID_LENGTH), unique=True, nullable=False, default=new_public_id
    )
    name: Mapped[str] = mapped_column(sa.String(NAME_MAX), nullable=False)
    hq_id: Mapped[str | None] = mapped_column(sa.String(HQ_ID_MAX), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="branches")

    @validates("public_id")
    def _public_id_is_permanent(self, key, value):
        return _keep_once_set(self, key, value)

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Branch {self.public_id} name={self.name!r}>"


class Installation(db.Model):
    __tablename__ = "installation"
    __table_args__ = (sa.CheckConstraint("id = 1", name="ck_installation_singleton"),)

    # Always 1, never generated: MySQL also refuses a CHECK on an AUTO_INCREMENT column.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    public_id: Mapped[str] = mapped_column(sa.String(UUID_LENGTH), nullable=False, default=new_public_id)
    branch_id: Mapped[int] = mapped_column(sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)

    branch: Mapped[Branch] = relationship()

    @validates("public_id")
    def _public_id_is_permanent(self, key, value):
        return _keep_once_set(self, key, value)

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Installation {self.public_id}>"
