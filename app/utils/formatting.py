"""Display formatting helpers registered as Jinja filters.

Presentation only. Nothing here is used to compute, compare or store a value:
money is `Decimal`/`DECIMAL(10,2)` end to end (docs/DATABASE.md), and this
module is the last step before a number reaches a template.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Flask

#: Nepali rupee, written the way Nepali menus and bills write it ("Rs. 250").
#: Named rather than inlined so there is one place to change it.
CURRENCY_SYMBOL = "Rs. "  # non-breaking space: an amount never wraps

#: Shown where a price is genuinely unknown -- e.g. a cart line whose menu item
#: was withdrawn. An em dash reads as "not applicable"; a zero would be a lie.
MISSING = "—"

_CENTS = Decimal("0.01")


def money(value) -> str:
    """Render a monetary amount Nepali-style: ``Rs. 249.00``, ``Rs. 1,23,456.78``.

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
    text = str(amount.quantize(_CENTS))
    sign = "-" if text.startswith("-") else ""
    whole, cents = text.lstrip("-").split(".")
    return f"{sign}{CURRENCY_SYMBOL}{_group(whole)}.{cents}"


def _group(digits: str) -> str:
    """South Asian digit grouping, as used in Nepal: the last three digits,
    then pairs -- 1234567 -> 12,34,567."""
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while head:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(pairs + [tail])


def register_filters(app: Flask) -> None:
    app.jinja_env.filters["money"] = money
