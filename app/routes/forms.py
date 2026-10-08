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

from app.models.category import SORT_ORDER_MAX
from app.models.enums import Cuisine, DietaryType, Locale, SpiceLevel
from app.models.restaurant_profile import ADDRESS_MAX, EMAIL_MAX, NAME_MAX, PHONE_MAX, TAGLINE_MAX
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


class PasswordChangeForm(FlaskForm):
    """M4. The policy itself is app/services/auth.py's; these validators only
    bound the input before Argon2 sees it."""

    current_password = PasswordField("Current password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)])
    password = PasswordField("New password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)])
    password_confirm = PasswordField("Confirm new password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)])
    submit = SubmitField("Change password")


class AccountSetupForm(FlaskForm):
    """M4: redeem an invitation or reset code. The code is a POST field, never
    part of a URL."""

    code = StringField("One-time code", validators=[DataRequired(), Length(max=128)])
    password = PasswordField("New password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)])
    password_confirm = PasswordField("Confirm new password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)])
    submit = SubmitField("Set password")


class InviteStaffForm(FlaskForm):
    """M4. No role field: invitations create STAFF accounts only."""

    username = StringField("Username", validators=[DataRequired(), Length(min=3, max=32)])
    email = StringField("Email", validators=[DataRequired(), Length(max=255), Email()])
    submit = SubmitField("Create invitation")


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=32)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)])
    submit = SubmitField("Log in")


def _sort_order_field():
    return IntegerField("Display order", default=0,
                        validators=[Optional(), NumberRange(min=0, max=SORT_ORDER_MAX)])


class CategoryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=500)])
    is_active = BooleanField("Active", default=True)
    sort_order = _sort_order_field()
    # M5: Nepali text for the customer menu. Blank = show the name above.
    name_ne = StringField("Name (नेपाली)", validators=[Optional(), Length(max=80)])
    description_ne = TextAreaField("Description (नेपाली)", validators=[Optional(), Length(max=500)])
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
    sort_order = _sort_order_field()
    name_ne = StringField("Name (नेपाली)", validators=[Optional(), Length(max=120)])
    description_ne = TextAreaField("Description (नेपाली)", validators=[Optional(), Length(max=2000)])
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
    # Plain strings: app/services/orders.py::resolve_order_channel is the one
    # validator for the source/table pair, so a raw POST gets the same rules.
    source = StringField(validators=[Optional(), Length(max=16)])
    table_id = StringField(validators=[Optional(), Length(max=12)])
    submit = SubmitField("Place order")


class StaffOrderForm(FlaskForm):
    """Staff order entry (M3). Only the order type and table are fields here;
    quantities arrive as ``qty_<menu item id>`` and are parsed by the route.
    Plain strings again: app/services/orders.py is the one validator."""

    source = StringField(validators=[Optional(), Length(max=16)])
    table_id = StringField(validators=[Optional(), Length(max=12)])


class CancelOrderForm(FlaskForm):
    reason = StringField("Reason (optional)", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Cancel order")


class TableForm(FlaskForm):
    name = StringField("Table name", validators=[DataRequired(), Length(max=40)])
    capacity = IntegerField("Seats", validators=[InputRequired(), NumberRange(min=1, max=50)])
    submit = SubmitField("Save table")


class PaymentForm(FlaskForm):
    """Method only -- the amount is always the order's server-side total."""

    method = StringField(validators=[DataRequired(), Length(max=16)])
    submit = SubmitField("Record payment")


class DiscountForm(FlaskForm):
    """Type, value and reason only -- the amount, tax and total are always
    computed server-side (app/services/pricing.py)."""

    discount_type = StringField(validators=[DataRequired(), Length(max=16)])
    discount_value = StringField("Discount", validators=[DataRequired(), Length(max=12)])
    discount_reason = StringField("Reason", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Apply discount")


class PricingSettingsForm(FlaskForm):
    tax_rate = StringField("Tax rate (%)", validators=[DataRequired(), Length(max=8)])
    staff_max_discount = StringField("Staff maximum discount (%)", validators=[DataRequired(), Length(max=8)])
    submit = SubmitField("Save settings")


class RestaurantProfileForm(FlaskForm):
    """M5. The service re-validates every field; there is no id field -- the
    profile is a single row the route never takes from the request."""

    name = StringField("Restaurant name", validators=[DataRequired(), Length(max=NAME_MAX)])
    tagline = StringField("Tagline", validators=[Optional(), Length(max=TAGLINE_MAX)])
    address = StringField("Address", validators=[Optional(), Length(max=ADDRESS_MAX)])
    phone = StringField("Phone", validators=[Optional(), Length(max=PHONE_MAX)])
    email = StringField("Email", validators=[Optional(), Length(max=EMAIL_MAX), Email()])
    default_locale = SelectField("Default menu language", choices=[(Locale.EN.value, "English"), (Locale.NE.value, "नेपाली")],
                                 coerce=_enum_coerce, validators=[DataRequired()])
    submit = SubmitField("Save restaurant profile")


class IngredientForm(FlaskForm):
    """Definition only -- quantities change through movements, never here."""

    name = StringField("Name", validators=[DataRequired(), Length(max=80)])
    unit = StringField("Unit", validators=[DataRequired(), Length(max=16)])
    minimum_quantity = StringField("Low-stock level", validators=[DataRequired(), Length(max=16)])
    is_active = BooleanField("Tracked in inventory", default=True)
    submit = SubmitField("Save ingredient")


class StockMovementForm(FlaskForm):
    movement_type = StringField(validators=[DataRequired(), Length(max=16)])
    quantity = StringField("Quantity", validators=[DataRequired(), Length(max=16)])
    note = StringField("Note", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Record")


class RecipeForm(FlaskForm):
    """CSRF carrier: the per-ingredient ``q_<id>`` quantities and the optional
    add-ingredient pair are parsed and validated by app/services/inventory.py."""

    submit = SubmitField("Save recipe")


class RefundForm(FlaskForm):
    amount = StringField("Refund amount", validators=[DataRequired(), Length(max=12)])
    reason = StringField("Reason", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Refund")


class ReservationForm(FlaskForm):
    """No owner field on purpose: the owner is the signed-in account. All four
    values are re-validated by app/services/reservations.py."""

    table_id = StringField(validators=[DataRequired(), Length(max=12)])
    reservation_date = StringField(validators=[DataRequired(), Length(max=10)])
    reservation_time = StringField(validators=[DataRequired(), Length(max=8)])
    guest_count = StringField(validators=[DataRequired(), Length(max=3)])
    submit = SubmitField("Book table")


class TableStatusForm(FlaskForm):
    status = StringField(validators=[DataRequired(), Length(max=16)])
    submit = SubmitField("Update")


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
    reason = StringField(validators=[Optional(), Length(max=255)])  # only used when cancelling
    submit = SubmitField("Update status")
