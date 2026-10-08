"""The restaurant profile (M5): the single place that reads or writes it.

``get_restaurant()`` returns the one profile row, cached for the request on
``flask.g``. If the row is missing (a database built with ``create_all()``,
as the test suite does) or the database cannot be read, it returns an
unsaved profile with the seeded defaults, so a page can always name the
restaurant. ``update_restaurant()`` is ADMIN only and audits what changed,
in the same transaction (``commit_with``, M2).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from flask import g
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.enums import Locale
from app.models.restaurant_profile import (
    ADDRESS_MAX,
    DEFAULT_RESTAURANT_NAME,
    EMAIL_MAX,
    NAME_MAX,
    PHONE_MAX,
    PROFILE_ID,
    TAGLINE_MAX,
    RestaurantProfile,
)
from app.models.user import Role, User
from app.services.audit import commit_with
from app.services.errors import ValidationError

logger = logging.getLogger(__name__)

_PHONE = re.compile(r"^[0-9+()\- ]{6,32}$")
_FIELDS = ("name", "tagline", "address", "phone", "email", "default_locale")


class RestaurantError(ValidationError):
    pass


def _default_profile() -> RestaurantProfile:
    return RestaurantProfile(id=PROFILE_ID, name=DEFAULT_RESTAURANT_NAME, default_locale=Locale.EN)


def get_restaurant() -> RestaurantProfile:
    if "restaurant" not in g:
        try:
            g.restaurant = db.session.get(RestaurantProfile, PROFILE_ID) or _default_profile()
        except SQLAlchemyError:
            # Pages (error pages included) must still render without the database.
            db.session.rollback()
            logger.warning("Could not read the restaurant profile; using defaults", exc_info=True)
            g.restaurant = _default_profile()
    return g.restaurant


@dataclass
class RestaurantInput:
    name: str | None
    tagline: str | None
    address: str | None
    phone: str | None
    email: str | None
    default_locale: str | None


def _clean(data: RestaurantInput) -> dict:
    from email_validator import EmailNotValidError, validate_email

    errors: dict[str, list[str]] = {}

    def text(field: str, limit: int, required: bool = False) -> str | None:
        value = (getattr(data, field) or "").strip() or None
        if value is None and required:
            errors[field] = ["This field is required."]
        elif value is not None and len(value) > limit:
            errors[field] = [f"Must be at most {limit} characters."]
        return value

    values = {
        "name": text("name", NAME_MAX, required=True),
        "tagline": text("tagline", TAGLINE_MAX),
        "address": text("address", ADDRESS_MAX),
        "phone": text("phone", PHONE_MAX),
        "email": text("email", EMAIL_MAX),
    }
    if values["phone"] and "phone" not in errors and not _PHONE.fullmatch(values["phone"]):
        errors["phone"] = ["Use digits, spaces and + ( ) - only."]
    if values["email"] and "email" not in errors:
        try:
            values["email"] = validate_email(values["email"], check_deliverability=False).normalized
        except EmailNotValidError:
            errors["email"] = ["Enter a valid email address."]
    try:
        values["default_locale"] = Locale(data.default_locale)
    except ValueError:
        errors["default_locale"] = ["Choose a supported language."]
    if errors:
        raise RestaurantError(errors)
    return values


def update_restaurant(actor: User, data: RestaurantInput, *, audit=None) -> dict:
    """Save the profile. Returns ``{field: {"from", "to"}}`` for the fields
    that changed (empty when nothing did -- then nothing is written or
    audited). ``audit(changed)`` adds the audit row in the same transaction."""
    if actor.role != Role.ADMIN:
        raise RestaurantError({"restaurant": ["Only an administrator can change the restaurant profile."]})
    values = _clean(data)

    row = db.session.get(RestaurantProfile, PROFILE_ID)
    current = row or _default_profile()

    def plain(value):
        return value.value if isinstance(value, Locale) else value

    changed = {f: {"from": plain(getattr(current, f)), "to": plain(values[f])}
               for f in _FIELDS if getattr(current, f) != values[f]}
    if not changed:
        return {}

    if row is None:
        row = RestaurantProfile(id=PROFILE_ID)
        db.session.add(row)
    for field in _FIELDS:
        setattr(row, field, values[field])
    row.updated_by_id = actor.id
    try:
        commit_with(audit, changed)
    except IntegrityError:
        db.session.rollback()
        raise RestaurantError({"restaurant": ["The profile was changed at the same time. Try again."]})
    finally:
        g.pop("restaurant", None)
    return changed
