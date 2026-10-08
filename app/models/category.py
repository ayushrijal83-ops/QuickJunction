"""Menu category (e.g. "Starters", "Beverages").

``name``/``description`` are the canonical (default-language) text; the
customer menu may show a ``CategoryTranslation`` instead (M5,
app/utils/locale.py::localized). ``sort_order`` (0 first) orders the public
menu and the management lists; ties fall back to name, then id.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import Locale, enum_column

SORT_ORDER_MAX = 10000


class Category(db.Model):
    __tablename__ = "categories"
    __table_args__ = (
        sa.CheckConstraint(f"sort_order >= 0 AND sort_order <= {SORT_ORDER_MAX}", name="ck_categories_sort_order_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0", default=0)

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    menu_items: Mapped[list["MenuItem"]] = relationship(back_populates="category")
    translations: Mapped[list["CategoryTranslation"]] = relationship(
        back_populates="category", cascade="all, delete-orphan", order_by="CategoryTranslation.locale"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Category id={self.id} name={self.name!r} active={self.is_active}>"


class CategoryTranslation(db.Model):
    """A category's name/description in one locale. At most one per locale
    (``uq_category_translations_category_locale``); the unique index also
    serves the foreign key."""

    __tablename__ = "category_translations"
    __table_args__ = (
        sa.UniqueConstraint("category_id", "locale", name="uq_category_translations_category_locale"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    locale: Mapped[Locale] = mapped_column(enum_column(Locale, 8, name="ck_category_translations_locale"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    category: Mapped[Category] = relationship(back_populates="translations")
