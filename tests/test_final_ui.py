"""Final UI pass: vendored assets, dish imagery, Nepali money format in pages,
confirmation dialogs on destructive forms, and the CSP left untouched.
Presentation tests -- no business rule is exercised here that other suites
do not already cover."""

from __future__ import annotations

from pathlib import Path

from flask import g

from app.models.payment import PaymentMethod
from app.models.user import Role
from app.services.payments import record_payment
from app.utils.headers import CONTENT_SECURITY_POLICY
from app.utils.imagery import SCENES, _BY_CUISINE, _FALLBACK, _KEYWORDS
from tests.conftest import make_user
from tests.test_restaurant_ops import customer, place
from tests.test_ui_overhaul import login, page

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_vendored_design_assets_are_served_from_this_origin(client):
    for path in ("/static/css/app.css", "/static/js/app.js", "/static/js/theme.js",
                 "/static/vendor/bootstrap-icons/bootstrap-icons.min.css",
                 "/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
                 "/static/fonts/inter-latin-wght-normal.woff2",
                 "/static/fonts/fraunces-latin-wght-normal.woff2",
                 "/static/img/favicon.svg"):
        assert client.get(path).status_code == 200, path


def test_every_image_the_helper_can_pick_exists_and_is_small():
    files = set(SCENES.values()) | set(_BY_CUISINE.values()) | {f for _, f in _KEYWORDS} | {_FALLBACK}
    for name in files:
        path = STATIC / "img" / name
        assert path.is_file(), name
        assert path.stat().st_size < 300_000, name  # optimised, never multi-MB


def test_csp_still_self_only_for_images_fonts_and_scripts():
    policy = dict(part.split(" ", 1) for part in CONTENT_SECURITY_POLICY.split("; "))
    assert policy["img-src"] == "'self' data:"
    assert policy["font-src"] == "'self'"
    assert policy["script-src"] == "'self'"


def test_menu_shows_dish_photos_and_nepali_prices(client, db):
    place(customer(), price="1234.00", qty=1)  # creates an available menu item
    g.pop("current_user", None)
    body = client.get("/menu").get_data(as_text=True)
    assert '/static/img/' in body and 'loading="lazy"' in body
    assert "Rs. 1,234.00" in body and "₹" not in body


def test_destructive_forms_ask_for_confirmation(client, db):
    login(client, "cust")
    from app.models.user import User
    from app.extensions import db as _db
    me = _db.session.query(User).filter_by(username="cust").one()
    order = place(me)
    assert 'data-confirm="Cancel order #%d?"' % order.id in page(client, f"/orders/{order.id}")

    boss = make_user(username="boss", email="boss@example.com", password="correct-horse-1", role=Role.ADMIN)
    record_payment(order, PaymentMethod.CASH.value, boss)
    client.post("/logout")
    login(client, "boss2", Role.ADMIN)
    assert 'data-confirm="Refund this payment?"' in page(client, f"/staff/orders/{order.id}")


def test_staff_and_admin_get_the_sidebar_shell_customers_do_not(client, db):
    login(client, "cust")
    assert 'id="sidebar"' not in page(client)
    client.post("/logout")
    login(client, "cook", Role.STAFF)
    body = page(client)
    assert 'id="sidebar"' in body and 'data-theme-toggle' in body
