"""Public landing page.

Added in M08: the application previously had no route at ``/``, so the site
root returned 404. Read-only and anonymous-safe -- it shows a few available
menu items and links into the existing flows, and computes nothing a caller
could influence.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from app.services.menu import list_public_menu_items

main_bp = Blueprint("main", __name__)

HOME_PREVIEW_LIMIT = 6


@main_bp.get("/")
def home():
    # Same "available item in an active category" filter as the public menu;
    # sliced for display only.
    items = list_public_menu_items()[:HOME_PREVIEW_LIMIT]
    return render_template("home.html", items=items)
