"""One preference row per customer.

Deliberately minimal: three nullable, allow-listed dimensions and nothing
else -- no name, phone, or address duplicated from ``User``. "Extensible"
per the brief means adding one more nullable column later, not a
key/value table now on the chance one is needed.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import Cuisine, DietaryType, SpiceLevel, enum_column


class CustomerPreference(db.Model):
    __tablename__ = "customer_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    dietary_preference: Mapped[DietaryType | None] = mapped_column(
        enum_column(DietaryType, 20, name="ck_customer_preferences_dietary_preference"), nullable=True
    )
    cuisine_preference: Mapped[Cuisine | None] = mapped_column(
        enum_column(Cuisine, 20, name="ck_customer_preferences_cuisine_preference"), nullable=True
    )
    spice_preference: Mapped[SpiceLevel | None] = mapped_column(
        enum_column(SpiceLevel, 16, name="ck_customer_preferences_spice_preference"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<CustomerPreference user_id={self.user_id}>"
