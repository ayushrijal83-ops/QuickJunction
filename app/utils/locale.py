"""Customer-menu language (M5): which locale a request is in, and the menu
text to show for it. Not a UI translation layer -- page chrome stays English.

Resolution, first match wins (``current_locale``):

0. a signed-in staff or admin user is always ``en`` (English console);
1. the session's ``locale``, set by the language switcher (POST /locale)
   or by ``?lang=en|ne`` on any page (``remember_lang_param``);
2. the browser's ``Accept-Language`` (``ne-NP`` counts as ``ne``);
3. the restaurant profile's ``default_locale``;
4. ``en``.

Only the values of ``Locale`` are ever stored or rendered; anything else is
ignored. The locale is a display preference, not an authority -- it gates no
data and no permission, so a ``?lang=`` GET may set it.

``localized(entity)`` gives a Category or MenuItem's name/description in the
current locale: the translation for that locale if it exists, otherwise the
canonical columns. A translation without a description falls back to the
canonical description, so text is never blank when the canonical row has it.
It also says which language each string is in (``lang``,
``description_lang``; canonical text is English), so a page can mark only
genuinely translated text ``lang="ne"`` while ``<html lang>`` stays ``en``
until the interface itself is translated (M6.1).
"""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit

from flask import Flask, g, request, session

from app.models.enums import Locale

SUPPORTED = tuple(locale.value for locale in Locale)
LABELS = {"en": "English", "ne": "नेपाली"}
_SESSION_KEY = "locale"


def normalize(value) -> str | None:
    value = value.strip().lower() if isinstance(value, str) else None
    return value if value in SUPPORTED else None


def remember_lang_param() -> None:
    """before_request: a valid ``?lang=`` becomes the session's choice."""
    chosen = normalize(request.args.get("lang"))
    if chosen and session.get(_SESSION_KEY) != chosen:
        session[_SESSION_KEY] = chosen


def set_locale(value) -> bool:
    chosen = normalize(value)
    if chosen:
        session[_SESSION_KEY] = chosen
        g.pop("locale", None)
    return chosen is not None


def current_locale() -> str:
    """Staff and admin always get ``en``: the operations console is English,
    and a ``ne`` choice kept across sign-in must not leak into it."""
    if "locale" not in g:
        from app.utils.authorization import get_current_user

        user = get_current_user()
        if user is not None and user.role.value in ("staff", "admin"):
            g.locale = Locale.EN.value
            return g.locale
        chosen = normalize(session.get(_SESSION_KEY)) or request.accept_languages.best_match(SUPPORTED)
        if not chosen:
            from app.services.restaurant import get_restaurant

            chosen = get_restaurant().default_locale.value
        g.locale = normalize(chosen) or Locale.EN.value
    return g.locale


CANONICAL = Locale.EN.value


class Text(NamedTuple):
    name: str
    description: str | None
    lang: str = CANONICAL
    description_lang: str = CANONICAL


def localized(entity, locale: str | None = None) -> Text:
    """Uses ``entity.translations`` -- eager-load it for lists (the public
    menu queries in app/services/menu.py do) to avoid a query per row."""
    locale = locale or current_locale()
    for translation in entity.translations:
        if translation.locale.value == locale and translation.name:
            return Text(translation.name, translation.description or entity.description, locale,
                        locale if translation.description else CANONICAL)
    return Text(entity.name, entity.description)


def safe_next(target: str | None, fallback: str) -> str:
    """A local path to return to, or ``fallback``. Refuses anything with a
    scheme or host, protocol-relative ``//`` and backslash tricks, and
    control characters -- so the switcher can never redirect off-site."""
    if not target or not isinstance(target, str) or len(target) > 2000:
        return fallback
    if any(ord(c) < 32 or c == "\\" for c in target):
        return fallback
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


def register_locale(app: Flask) -> None:
    app.before_request(remember_lang_param)
    app.jinja_env.globals.update(
        current_locale=current_locale, localized=localized, locale_labels=LABELS,
    )
