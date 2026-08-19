"""Display formatting helpers registered as Jinja filters.

Presentation only. Nothing here is used to compute, compare or store a value:
money is `Decimal`/`DECIMAL(10,2)` end to end (docs/DATABASE.md), and this
module is the last step before a number reaches a template.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Flask

#: Rupee sign. Named rather than inlined so there is one place to change it.
CURRENCY_SYMBOL = "₹"

#: Shown where a price is genuinely unknown -- e.g. a cart line whose menu item
#: was withdrawn. An em dash reads as "not applicable"; a zero would be a lie.
MISSING = "—"

_CENTS = Decimal("0.01")


def money(value) -> str:
    """Render a monetary amount as ``₹249.00``.

    Stays inside :class:`~decimal.Decimal` throughout. A value that arrives as
    a string or int is converted via ``str()``, never through ``float()``,
    because binary floating point cannot represent most decimal fractions
    exactly and this is the one place where being a cent out is visible to a
    customer.

    ``None`` renders as an em dash rather than raising, so a template can pass
    a nullable column straight in.
    """
    if value is None or value == "":
        return MISSING

    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return MISSING

    # quantize() rounds half-to-even by default, which is the right behaviour
    # for display: these values are already stored to two places, so this only
    # normalises presentation (249.5 -> 249.50) rather than altering money.
    return f"{CURRENCY_SYMBOL}{amount.quantize(_CENTS)}"


def register_filters(app: Flask) -> None:
    app.jinja_env.filters["money"] = money
