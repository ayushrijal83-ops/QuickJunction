"""Controlled vocabularies shared by menu items and customer preferences.

Reused (not duplicated) between the two so a preference and a menu item's
attribute are directly comparable -- the join the future recommendation
engine needs. Kept as Python enums (not free text) because the brief
requires allow-list validation for these dimensions, both in the form layer
and at the database via a CHECK constraint (see app/models/menu_item.py).
"""

from __future__ import annotations

import enum

import sqlalchemy as sa


class SpiceLevel(str, enum.Enum):
    NONE = "none"
    MILD = "mild"
    MEDIUM = "medium"
    HOT = "hot"
    EXTRA_HOT = "extra_hot"


class DietaryType(str, enum.Enum):
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    EGGETARIAN = "eggetarian"
    NON_VEGETARIAN = "non_vegetarian"


class Cuisine(str, enum.Enum):
    INDIAN = "indian"
    CHINESE = "chinese"
    ITALIAN = "italian"
    CONTINENTAL = "continental"
    MEXICAN = "mexican"
    THAI = "thai"
    MULTI_CUISINE = "multi_cuisine"
    OTHER = "other"


def enum_column(enum_cls: type[enum.Enum], length: int, *, name: str) -> sa.Enum:
    """A constrained-string column (VARCHAR + CHECK, not a native DB enum)
    for one of the classes above.

    Four things have to be true together, and SQLAlchemy defaults three of
    them the other way:

    - ``native_enum=False`` -- a VARCHAR, not a DB-native ENUM type, so
      adding a value later is a plain constraint migration.
    - ``create_constraint=True`` -- **not the default** as of SQLAlchemy
      2.0. Without it, ``native_enum=False`` silently produces a bare
      VARCHAR with no database-level enforcement at all -- caught during
      this project by inspecting the actual emitted DDL, not by reading
      the docs.
    - ``values_callable=...`` -- stores ``member.value`` (e.g.
      ``"admin"``); without it SQLAlchemy stores ``member.name``
      (``"ADMIN"``), which then disagrees with any ``server_default``
      written as the value.
    - an explicit, caller-supplied ``name`` -- left to its default, the
      constraint is named after the *enum class* (e.g. ``"cuisine"``), not
      the column. ``Cuisine`` backs both ``menu_items.cuisine`` and
      ``customer_preferences.cuisine_preference``, so two columns would
      generate the identical constraint name. SQLite tolerates that
      (constraint names are only scoped per-table there); MySQL requires
      ``CHECK`` constraint names to be unique **per schema**, so the second
      ``CREATE TABLE`` would fail outright. Always name it
      ``ck_<table>_<column>``.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
