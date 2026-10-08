"""Menu item.

Price is ``Numeric(10, 2)`` end to end -- never ``float`` -- and mapped to
Python's ``Decimal``, so no binary floating-point rounding can ever enter a
price anywhere between the database and a template. A ``CHECK`` constraint
backs up the server-side validation in ``app/services/menu.py``: even a
direct/careless insert cannot create a zero or negative price.

``cuisine``, ``spice_level`` and ``dietary_type`` are the three allow-listed
dimensions the brief calls out; each is a Python enum stored as a
constrained string (see app/models/enums.py) and is the same vocabulary
``CustomerPreference`` uses, which is what will let a future recommendation
engine compare the two directly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.category import SORT_ORDER_MAX
from app.models.enums import Cuisine, DietaryType, Locale, SpiceLevel, enum_column
from app.models.ingredient import Ingredient
from app.models.menu_item_ingredient import MenuItemIngredient


class MenuItem(db.Model):
    __tablename__ = "menu_items"
    __table_args__ = (
        sa.CheckConstraint("price > 0", name="ck_menu_items_price_positive"),
        # Backs the public menu's most common query: items in a given
        # category that are currently available.
        sa.Index("ix_menu_items_category_available", "category_id", "is_available"),
        sa.CheckConstraint(f"sort_order >= 0 AND sort_order <= {SORT_ORDER_MAX}", name="ck_menu_items_sort_order_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # No standalone index: the composite index below already covers a
    # category-only lookup via its leftmost column.
    category_id: Mapped[int] = mapped_column(
        sa.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(2000), nullable=True)

    price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)

    cuisine: Mapped[Cuisine] = mapped_column(
        enum_column(Cuisine, 20, name="ck_menu_items_cuisine"), nullable=False
    )
    spice_level: Mapped[SpiceLevel] = mapped_column(
        enum_column(SpiceLevel, 16, name="ck_menu_items_spice_level"), nullable=False
    )
    dietary_type: Mapped[DietaryType] = mapped_column(
        enum_column(DietaryType, 20, name="ck_menu_items_dietary_type"), nullable=False
    )

    is_available: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    # M5: position within its category (0 first; ties by name, then id).
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0", default=0)

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    category: Mapped["Category"] = relationship(back_populates="menu_items")  # noqa: F821
    ingredients: Mapped[list[Ingredient]] = relationship(
        secondary=MenuItemIngredient.__table__, order_by=Ingredient.name
    )
    translations: Mapped[list["MenuItemTranslation"]] = relationship(
        back_populates="menu_item", cascade="all, delete-orphan", order_by="MenuItemTranslation.locale"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<MenuItem id={self.id} name={self.name!r} price={self.price} available={self.is_available}>"


class MenuItemTranslation(db.Model):
    """A menu item's name/description in one locale (M5), shown on the
    customer menu only. Orders, the kitchen, inventory, recommendations and
    the local LLM keep using the canonical ``MenuItem.name``/``description``,
    and an order line's ``item_name_snapshot`` never changes."""

    __tablename__ = "menu_item_translations"
    __table_args__ = (
        sa.UniqueConstraint("menu_item_id", "locale", name="uq_menu_item_translations_item_locale"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    menu_item_id: Mapped[int] = mapped_column(sa.ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=False)
    locale: Mapped[Locale] = mapped_column(
        enum_column(Locale, 8, name="ck_menu_item_translations_locale"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(2000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    menu_item: Mapped[MenuItem] = relationship(back_populates="translations")
