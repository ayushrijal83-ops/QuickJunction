"""MenuItem <-> Ingredient join table, and (since M13) the recipe.

The composite primary key enforces "a menu item lists an ingredient at most
once". ``quantity`` is how much of the ingredient one portion uses, in the
ingredient's unit. 0 (the server default) means "listed as an attribute for
recommendations, not stock-tracked" -- which is what every link created by
the menu form's comma-separated ingredient list is, so that form keeps
working unchanged and never erases a recipe amount (SQLAlchemy only inserts
or deletes the association rows that actually change).
"""

from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


class MenuItemIngredient(db.Model):
    __tablename__ = "menu_item_ingredients"
    __table_args__ = (sa.CheckConstraint("quantity >= 0", name="ck_menu_item_ingredients_quantity_nonnegative"),)

    menu_item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("menu_items.id", ondelete="CASCADE"), primary_key=True
    )
    ingredient_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ingredients.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 3), nullable=False, server_default="0", default=Decimal("0")
    )

    ingredient: Mapped["Ingredient"] = relationship(viewonly=True)  # noqa: F821
