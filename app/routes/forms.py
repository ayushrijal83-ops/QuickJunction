"""WTForms definitions for the auth and menu-management views.

Validators run server-side regardless of what any client-side script does
or skips -- that is the whole point of using them here instead of trusting
the browser. The <select> fields below are one layer of allow-list
enforcement, not the only one: app/services/menu.py re-validates every
enumerated value against its enum independently, because a raw POST can
always skip the <select>.
"""

from __future__ import annotations

import enum

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DecimalField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, InputRequired, Length, NumberRange, Optional, Regexp

from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.services.cart import MAX_QUANTITY_PER_ITEM, MIN_QUANTITY
from app.services.menu import MAX_PRICE, MIN_PRICE
from app.utils.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH


def _enum_choices(enum_cls) -> list[tuple[str, str]]:
    return [(member.value, member.value.replace("_", " ").title()) for member in enum_cls]


def _enum_coerce(value):
    """WTForms' default ``coerce=str`` turns a str-Enum member into
    ``"ClassName.MEMBER"`` (Python's Enum.__str__, not str.__str__ --
    verified: it wins even though ``str`` is the first base), breaking a
    pre-filled edit form's selection. ``.value`` handles a model object
    populating the form; plain ``str()`` handles a raw POSTed form string,
    which is already the right value."""
    return value.value if isinstance(value, enum.Enum) else str(value)


class RegistrationForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[
            DataRequired(),
            Length(min=3, max=32),
            Regexp(r"^[a-zA-Z0-9_]+$", message="Username may only contain letters, digits, and underscores."),
        ],
    )
    email = StringField("Email", validators=[DataRequired(), Length(max=255), Email()])
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(min=PASSWORD_MIN_LENGTH, max=PASSWORD_MAX_LENGTH)]
    )
    password_confirm = PasswordField("Confirm password", validators=[DataRequired()])
    submit = SubmitField("Register")


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=32)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)])
    submit = SubmitField("Log in")


class CategoryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=500)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save category")


class MenuItemForm(FlaskForm):
    # choices populated per-request in the route (categories are dynamic
    # data, not a fixed vocabulary like the enum fields below).
    category_id = SelectField("Category", coerce=int, validators=[InputRequired()])

    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=2000)])
    price = DecimalField(
        "Price", places=2, validators=[InputRequired(), NumberRange(min=MIN_PRICE, max=MAX_PRICE)]
    )
    cuisine = SelectField(
        "Cuisine", choices=_enum_choices(Cuisine), coerce=_enum_coerce, validators=[DataRequired()]
    )
    spice_level = SelectField(
        "Spice level", choices=_enum_choices(SpiceLevel), coerce=_enum_coerce, validators=[DataRequired()]
    )
    dietary_type = SelectField(
        "Dietary type", choices=_enum_choices(DietaryType), coerce=_enum_coerce, validators=[DataRequired()]
    )
    is_available = BooleanField("Available", default=True)
    ingredients = StringField(
        "Ingredients (comma-separated)", validators=[Optional(), Length(max=1000)]
    )
    submit = SubmitField("Save menu item")


class AddToCartForm(FlaskForm):
    menu_item_id = IntegerField(validators=[InputRequired()])
    quantity = IntegerField(
        default=1, validators=[InputRequired(), NumberRange(min=MIN_QUANTITY, max=MAX_QUANTITY_PER_ITEM)]
    )
    submit = SubmitField("Add to cart")


class UpdateCartForm(FlaskForm):
    menu_item_id = IntegerField(validators=[InputRequired()])
    quantity = IntegerField(validators=[InputRequired(), NumberRange(min=MIN_QUANTITY, max=MAX_QUANTITY_PER_ITEM)])
    submit = SubmitField("Update")


class RemoveFromCartForm(FlaskForm):
    menu_item_id = IntegerField(validators=[InputRequired()])
    submit = SubmitField("Remove")


class ClearCartForm(FlaskForm):
    submit = SubmitField("Clear cart")


class CheckoutForm(FlaskForm):
    submit = SubmitField("Place order")


def _optional_enum_choices(enum_cls) -> list[tuple[str, str]]:
    """Enum choices plus an explicit "no preference" option. Empty string,
    not a sentinel member -- "not stated" is stored as NULL."""
    return [("", "No preference")] + _enum_choices(enum_cls)


class PreferenceForm(FlaskForm):
    """All three fields optional: a customer may state some, all, or none.
    The service layer (app/services/preferences.py) re-validates every value
    against its enum independently, since a raw POST can skip the <select>."""

    dietary_preference = SelectField(
        "Dietary preference", choices=_optional_enum_choices(DietaryType),
        coerce=_enum_coerce, validators=[Optional()],
    )
    cuisine_preference = SelectField(
        "Preferred cuisine", choices=_optional_enum_choices(Cuisine),
        coerce=_enum_coerce, validators=[Optional()],
    )
    spice_preference = SelectField(
        "Spice preference", choices=_optional_enum_choices(SpiceLevel),
        coerce=_enum_coerce, validators=[Optional()],
    )
    submit = SubmitField("Save preferences")


class OrderStatusForm(FlaskForm):
    """Carries only the target status. The *current* status is read from the
    database row, never from the form -- so a stale or forged page cannot
    talk the server into a transition the real row does not permit."""

    status = StringField(validators=[DataRequired(), Length(max=16)])
    submit = SubmitField("Update status")
