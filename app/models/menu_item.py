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
from app.models.enums import Cuisine, DietaryType, SpiceLevel, enum_column
from app.models.ingredient import Ingredient
from app.models.menu_item_ingredient import MenuItemIngredient


class MenuItem(db.Model):
    __tablename__ = "menu_items"
    __table_args__ = (
        sa.CheckConstraint("price > 0", name="ck_menu_items_price_positive"),
        # Backs the public menu's most common query: items in a given
        # category that are currently available.
        sa.Index("ix_menu_items_category_available", "category_id", "is_available"),
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

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    category: Mapped["Category"] = relationship(back_populates="menu_items")  # noqa: F821
    ingredients: Mapped[list[Ingredient]] = relationship(
        secondary=MenuItemIngredient.__table__, order_by=Ingredient.name
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<MenuItem id={self.id} name={self.name!r} price={self.price} available={self.is_available}>"
