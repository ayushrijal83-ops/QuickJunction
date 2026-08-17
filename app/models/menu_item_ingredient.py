"""MenuItem <-> Ingredient join table.

No columns beyond the two foreign keys, so the composite primary key both
enforces "a menu item lists an ingredient at most once" and is the table's
only index -- no separate uniqueness constraint needed. ``ondelete="CASCADE"``
on both sides means deleting a menu item or an ingredient cleans up the
association automatically instead of leaving an orphaned row.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class MenuItemIngredient(db.Model):
    __tablename__ = "menu_item_ingredients"

    menu_item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("menu_items.id", ondelete="CASCADE"), primary_key=True
    )
    ingredient_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ingredients.id", ondelete="CASCADE"), primary_key=True, index=True
    )
