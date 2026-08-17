"""Customer taste preferences.

One row per customer (``CustomerPreference``, added in M03), created lazily
the first time a customer saves anything. All three dimensions are optional:
"no preference stated" is a meaningful answer and is stored as ``NULL``,
not as a sentinel enum member.

The vocabulary is the *same* ``DietaryType`` / ``Cuisine`` / ``SpiceLevel``
enums that ``MenuItem`` uses (app/models/enums.py) -- not a parallel set --
which is what lets app/services/recommendations.py compare a preference
against a menu item without a translation layer.

No ``flask.request`` or ``flask.session`` here: callers pass a ``user_id``
they have already authenticated. Nothing in this module accepts a user id
from a client.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.extensions import db
from app.models.customer_preference import CustomerPreference
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.services.errors import ValidationError


class PreferenceError(ValidationError):
    pass


@dataclass
class PreferenceInput:
    """Raw strings as they arrive from a form. Empty string means "cleared";
    every non-empty value is re-validated against its enum here, because a
    raw POST can always skip the <select>."""

    dietary_preference: str | None = None
    cuisine_preference: str | None = None
    spice_preference: str | None = None


_FIELDS = (
    ("dietary_preference", DietaryType),
    ("cuisine_preference", Cuisine),
    ("spice_preference", SpiceLevel),
)


def _coerce(raw: str | None, enum_cls, field: str, errors: dict[str, list[str]]):
    """`None`/`""` -> None (cleared). Anything else must be a real member."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        errors.setdefault(field, []).append(f"Invalid {field.replace('_', ' ')}.")
        return None


def get_preferences(user_id: int) -> CustomerPreference | None:
    return db.session.query(CustomerPreference).filter_by(user_id=user_id).first()


def update_preferences(user_id: int, data: PreferenceInput) -> CustomerPreference:
    """Create or update the row belonging to ``user_id``.

    The row is located *by* ``user_id``, never by a client-supplied row id --
    there is no code path here through which one customer can reach another
    customer's preferences.
    """
    errors: dict[str, list[str]] = {}
    values = {
        field: _coerce(getattr(data, field), enum_cls, field, errors)
        for field, enum_cls in _FIELDS
    }
    if errors:
        raise PreferenceError(errors)

    preference = get_preferences(user_id)
    if preference is None:
        preference = CustomerPreference(user_id=user_id)
        db.session.add(preference)

    for field, value in values.items():
        setattr(preference, field, value)

    db.session.commit()
    return preference
