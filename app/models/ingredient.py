"""Ingredient -- a reference/lookup table, not a mutable business entity,
so unlike Category/MenuItem it carries no ``updated_at``."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class Ingredient(db.Model):
    __tablename__ = "ingredients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Ingredient id={self.id} name={self.name!r}>"
