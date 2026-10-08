"""Public landing page.

Added in M08: the application previously had no route at ``/``, so the site
root returned 404. Read-only and anonymous-safe -- it shows a few available
menu items and links into the existing flows, and computes nothing a caller
could influence.

M5 adds ``POST /locale``, the customer menu's language switcher: ``lang``
must be ``en`` or ``ne`` (anything else is a 400) and ``next`` must be a
local path, otherwise the menu is used (app/utils/locale.py::safe_next) --
so it can never become an open redirect. CSRF: app-wide CSRFProtect.
"""

from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from app.services.menu import list_public_menu_items
from app.utils.locale import safe_next, set_locale

main_bp = Blueprint("main", __name__)

HOME_PREVIEW_LIMIT = 6


@main_bp.get("/")
def home():
    # Same "available item in an active category" filter as the public menu;
    # sliced for display only.
    items = list_public_menu_items()[:HOME_PREVIEW_LIMIT]
    return render_template("home.html", items=items)


@main_bp.post("/locale")
def switch_locale():
    if not set_locale(request.form.get("lang")):
        abort(400)
    return redirect(safe_next(request.form.get("next"), url_for("menu.index")), code=303)
