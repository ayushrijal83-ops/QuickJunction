"""The installed restaurant's identity (M5): the business the customer sees.

Quick Junction is the software; this row is the restaurant running it. A
single row (id = 1, enforced by ``ck_restaurant_profile_singleton``), created
by migration b1c4e7f9a2d5. Until an admin enters the restaurant's own details
at ``/admin/settings/restaurant`` the row holds only the name the site already
showed before M5 ("Quick Junction") and no contact details -- no invented
restaurant, no demo text. It is read through
``app/services/restaurant.py::get_restaurant`` (which falls back to the same
values if the row is missing).

Deliberately not a tenant: nothing else references this table, and no
other table carries a ``restaurant_id``.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import Locale, enum_column

PROFILE_ID = 1
# Not a restaurant: the name every customer page used before M5, kept until
# the admin sets the real one, so upgrading changes nothing customers see.
DEFAULT_RESTAURANT_NAME = "Quick Junction"

NAME_MAX = 120
TAGLINE_MAX = 200
ADDRESS_MAX = 255
PHONE_MAX = 32
EMAIL_MAX = 255


class RestaurantProfile(db.Model):
    __tablename__ = "restaurant_profile"
    __table_args__ = (sa.CheckConstraint("id = 1", name="ck_restaurant_profile_singleton"),)

    # Always 1, never generated: MySQL also refuses a CHECK on an AUTO_INCREMENT column.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(sa.String(NAME_MAX), nullable=False)
    tagline: Mapped[str | None] = mapped_column(sa.String(TAGLINE_MAX), nullable=True)
    address: Mapped[str | None] = mapped_column(sa.String(ADDRESS_MAX), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.String(PHONE_MAX), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(EMAIL_MAX), nullable=True)
    # Shown when neither the visitor nor their browser has chosen a language.
    default_locale: Mapped[Locale] = mapped_column(
        enum_column(Locale, 8, name="ck_restaurant_profile_default_locale"),
        nullable=False,
        server_default=Locale.EN.value,
    )
    updated_by_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<RestaurantProfile name={self.name!r}>"
