"""M5 -- menu & restaurant foundation.

Restaurant profile, display ordering, English/Nepali menu content, the
customer-menu locale, staff sold-out control and the shared public-menu
rule. Every test here also runs on MySQL when TEST_DATABASE_URL points at
one (the Devanagari round trips are meant for that run). The migration
round trip is in tests/test_m5_migration.py; the concurrent sold-out race in
tests/test_mysql_concurrency.py.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import sqlalchemy as sa
from flask import g
from sqlalchemy.exc import IntegrityError

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.category import Category, CategoryTranslation
from app.models.enums import Locale
from app.models.menu_item import MenuItem, MenuItemTranslation
from app.models.order import OrderItem
from app.models.restaurant_profile import DEFAULT_RESTAURANT_NAME, RestaurantProfile
from app.models.user import Role
from app.services.cart import get_available_menu_item
from app.services.menu import (
    MenuItemError,
    MenuItemInput,
    create_menu_item,
    get_public_menu_item,
    list_admin_categories,
    list_admin_menu_items,
    list_public_categories,
    list_public_menu_items,
    set_availability,
    update_menu_item,
)
from app.services.orders import CheckoutError, checkout
from app.services.recommendations import build_item_document
from app.services.restaurant import RestaurantError, RestaurantInput, get_restaurant, update_restaurant
from app.utils.locale import localized, safe_next
from tests.conftest import make_category, make_menu_item, make_user, rejected_by_check_constraint

PASSWORD = "correct-horse-1"
NEPALI_NAME = "पनिर टिक्का"
NEPALI_DESC = "तन्दुरमा पकाइएको मसालेदार पनिर।"
NEPALI_CATEGORY = "खाजा"


def req(client, method, url, **kw):
    """Each request as its own identity (known issue #29)."""
    g.pop("current_user", None)
    g.pop("restaurant", None)
    g.pop("locale", None)
    return getattr(client, method)(url, **kw)


def signed_in(app, username, role=Role.CUSTOMER):
    account = make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)
    client = app.test_client()
    assert req(client, "post", "/login", data={"username": username, "password": PASSWORD}).status_code == 302
    return client, account


def body(response) -> str:
    return response.get_data(as_text=True)


def audit(event: AuditEvent) -> list[AuditLog]:
    _db.session.expire_all()
    return _db.session.query(AuditLog).filter_by(event_type=event).all()


def add_translation(entity, locale="ne", name=NEPALI_NAME, description=NEPALI_DESC):
    model = MenuItemTranslation if isinstance(entity, MenuItem) else CategoryTranslation
    entity.translations.append(model(locale=Locale(locale), name=name, description=description))
    _db.session.commit()


def seed_profile(**kw) -> RestaurantProfile:
    profile = RestaurantProfile(id=1, name=kw.pop("name", "Himalayan Test Kitchen"),
                                default_locale=kw.pop("default_locale", Locale.EN), **kw)
    _db.session.add(profile)
    _db.session.commit()
    g.pop("restaurant", None)
    return profile


# =============================================================================
# restaurant profile
# =============================================================================


def test_get_restaurant_falls_back_to_demo_defaults_without_a_row(app, db):
    profile = get_restaurant()
    assert profile.name == DEFAULT_RESTAURANT_NAME and profile.default_locale == Locale.EN
    assert _db.session.query(RestaurantProfile).count() == 0  # the fallback is never saved


def test_get_restaurant_reads_the_row_once_per_request(app, db):
    seed_profile()
    assert get_restaurant() is get_restaurant()
    assert get_restaurant().name == "Himalayan Test Kitchen"


def test_profile_is_a_single_row(db):
    seed_profile()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("INSERT INTO restaurant_profile (id, name, default_locale, created_at, updated_at)"
                                    " VALUES (2, 'Second', 'en', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        _db.session.commit()
    _db.session.rollback()


def test_profile_default_locale_vocabulary_is_enforced(db):
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("INSERT INTO restaurant_profile (id, name, default_locale, created_at, updated_at)"
                                    " VALUES (1, 'X', 'fr', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        _db.session.commit()
    _db.session.rollback()


def profile_form(**overrides):
    data = {"name": "Thakali Ghar", "tagline": "Dal bhat, done properly", "address": "Lakeside, Pokhara",
            "phone": "+977 61 000000", "email": "hello@thakali.example", "default_locale": "ne"}
    data.update(overrides)
    return data


def test_admin_updates_profile_and_the_change_is_audited(app, db):
    seed_profile()
    client, admin = signed_in(app, "boss", Role.ADMIN)
    assert req(client, "get", "/admin/settings/restaurant").status_code == 200
    response = req(client, "post", "/admin/settings/restaurant", data=profile_form())
    assert response.status_code == 302
    _db.session.expire_all()
    profile = _db.session.get(RestaurantProfile, 1)
    assert (profile.name, profile.phone, profile.default_locale, profile.updated_by_id) == (
        "Thakali Ghar", "+977 61 000000", Locale.NE, admin.id)
    [row] = audit(AuditEvent.RESTAURANT_PROFILE_CHANGED)
    changed = json.loads(row.metadata_json)["changed"]
    assert row.user_id == admin.id and row.success
    assert changed["name"] == {"from": "Himalayan Test Kitchen", "to": "Thakali Ghar"}
    assert changed["default_locale"] == {"from": "en", "to": "ne"}


def test_saving_an_unchanged_profile_writes_no_audit_row(app, db):
    client, _ = signed_in(app, "boss", Role.ADMIN)
    req(client, "post", "/admin/settings/restaurant", data=profile_form())
    req(client, "post", "/admin/settings/restaurant", data=profile_form())
    assert len(audit(AuditEvent.RESTAURANT_PROFILE_CHANGED)) == 1


def test_profile_row_is_created_on_first_save_when_missing(app, db):
    client, _ = signed_in(app, "boss", Role.ADMIN)
    req(client, "post", "/admin/settings/restaurant", data=profile_form())
    assert _db.session.query(RestaurantProfile).one().name == "Thakali Ghar"


@pytest.mark.parametrize("role, status", [(Role.STAFF, 403), (Role.CUSTOMER, 403), (None, 401)])
def test_only_admins_reach_the_profile(app, db, role, status):
    seed_profile()
    client = signed_in(app, "someone", role)[0] if role else app.test_client()
    assert req(client, "get", "/admin/settings/restaurant").status_code == status
    assert req(client, "post", "/admin/settings/restaurant", data=profile_form()).status_code == status
    _db.session.expire_all()
    assert _db.session.get(RestaurantProfile, 1).name == "Himalayan Test Kitchen"
    assert audit(AuditEvent.RESTAURANT_PROFILE_CHANGED) == []


def test_profile_service_refuses_non_admin_actors(app, db):
    staff = make_user(username="cook", email="cook@example.com", role=Role.STAFF)
    with pytest.raises(RestaurantError):
        update_restaurant(staff, RestaurantInput("X", None, None, None, None, "en"))


def test_profile_post_cannot_target_another_row(app, db):
    seed_profile()
    client, _ = signed_in(app, "boss", Role.ADMIN)
    req(client, "post", "/admin/settings/restaurant", data=profile_form(id="2", updated_by_id="999"))
    rows = _db.session.query(RestaurantProfile).all()
    assert [r.id for r in rows] == [1] and rows[0].updated_by_id != 999


@pytest.mark.parametrize("field, value", [
    ("name", "   "), ("name", "x" * 121), ("email", "not-an-email"), ("phone", "call me maybe"),
    ("default_locale", "fr"), ("tagline", "x" * 201),
])
def test_invalid_profile_input_changes_nothing(app, db, field, value):
    seed_profile()
    client, _ = signed_in(app, "boss", Role.ADMIN)
    req(client, "post", "/admin/settings/restaurant", data=profile_form(**{field: value}))
    _db.session.expire_all()
    assert _db.session.get(RestaurantProfile, 1).name == "Himalayan Test Kitchen"
    assert audit(AuditEvent.RESTAURANT_PROFILE_CHANGED) == []


def test_profile_update_rolls_back_when_audit_fails(app, db, monkeypatch):
    seed_profile()
    client, _ = signed_in(app, "boss", Role.ADMIN)

    def boom(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr("app.routes.admin_settings.record_event", boom)
    assert req(client, "post", "/admin/settings/restaurant", data=profile_form()).status_code == 500
    _db.session.expire_all()
    assert _db.session.get(RestaurantProfile, 1).name == "Himalayan Test Kitchen"


def test_profile_post_requires_csrf(app, db):
    seed_profile()
    client, _ = signed_in(app, "boss", Role.ADMIN)
    app.config["WTF_CSRF_ENABLED"] = True
    assert req(client, "post", "/admin/settings/restaurant", data=profile_form()).status_code == 400
    _db.session.expire_all()
    assert _db.session.get(RestaurantProfile, 1).name == "Himalayan Test Kitchen"


def test_customer_pages_show_the_restaurant_and_ops_pages_the_software(app, client, db):
    seed_profile(name="Thakali Ghar", tagline="Dal bhat, done properly", phone="+977 61 000000")
    home = body(req(client, "get", "/"))
    assert "<title>Thakali Ghar &mdash; Dal bhat, done properly</title>" in home
    assert 'aria-label="Thakali Ghar home"' in home
    assert "+977 61 000000" in home and "Powered by Quick Junction" in home
    menu = body(req(client, "get", "/menu"))
    assert "<title>Menu &mdash; Thakali Ghar</title>" in menu

    staff_client, _ = signed_in(app, "cook", Role.STAFF)
    ops = body(req(staff_client, "get", "/staff/menu"))
    assert 'aria-label="Quick Junction home"' in ops and "Thakali Ghar &middot; Operations console" in ops


def test_restaurant_identity_is_escaped(app, client, db):
    seed_profile(name="<script>alert(1)</script>", tagline='"><img src=x onerror=alert(1)>')
    page = body(req(client, "get", "/"))
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# =============================================================================
# display ordering
# =============================================================================


def ordered_menu():
    """Three categories and items whose sort_order disagrees with A-Z."""
    drinks = Category(name="Drinks", sort_order=2)
    mains = Category(name="Mains", sort_order=1)
    starters = Category(name="Starters", sort_order=1)  # ties with Mains: name decides
    _db.session.add_all([drinks, mains, starters])
    _db.session.commit()
    items = {
        "Zebra Momo": make_menu_item(category=mains, name="Zebra Momo"),
        "Apple Curry": make_menu_item(category=mains, name="Apple Curry"),
        "Lassi": make_menu_item(category=drinks, name="Lassi"),
        "Soup": make_menu_item(category=starters, name="Soup"),
    }
    items["Zebra Momo"].sort_order = 0
    items["Apple Curry"].sort_order = 5
    _db.session.commit()
    return items


EXPECTED = ["Zebra Momo", "Apple Curry", "Soup", "Lassi"]


def test_public_menu_follows_category_then_item_order(db):
    ordered_menu()
    assert [i.name for i in list_public_menu_items()] == EXPECTED
    assert [c.name for c in list_public_categories()] == ["Mains", "Starters", "Drinks"]


def test_management_lists_use_the_same_order(app, db):
    ordered_menu()
    assert [i.name for i in list_admin_menu_items()] == EXPECTED
    assert [c.name for c in list_admin_categories()] == ["Mains", "Starters", "Drinks"]
    client, _ = signed_in(app, "cook", Role.STAFF)
    page = body(req(client, "get", "/staff/menu"))
    assert [page.index(n) for n in EXPECTED] == sorted(page.index(n) for n in EXPECTED)


def test_equal_sort_order_and_equal_names_tie_break_by_id(db):
    a, b = Category(name="A"), Category(name="B")
    _db.session.add_all([a, b])
    _db.session.commit()
    first = make_menu_item(category=a, name="Same")
    second = make_menu_item(category=b, name="Same")
    b.sort_order = a.sort_order = 0
    _db.session.commit()
    assert [i.id for i in list_public_menu_items()] == [first.id, second.id]


def test_existing_rows_default_to_order_zero(db):
    item = make_menu_item()
    _db.session.expire_all()
    assert item.sort_order == 0 and item.category.sort_order == 0


def test_public_order_is_rendered_on_the_menu_page(client, db):
    ordered_menu()
    page = body(req(client, "get", "/menu"))
    assert [page.index(n) for n in EXPECTED] == sorted(page.index(n) for n in EXPECTED)


@pytest.mark.parametrize("value", [-1, 10001, "abc", "1.5"])
def test_out_of_range_sort_order_is_refused_by_the_service(db, value):
    category = make_category()
    with pytest.raises(MenuItemError) as exc:
        create_menu_item(MenuItemInput(category.id, "Momo", None, "150", "indian", "mild", "vegetarian",
                                       sort_order=value))
    assert "sort_order" in exc.value.errors


def test_out_of_range_sort_order_is_refused_by_the_database(db):
    item = make_menu_item()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("UPDATE menu_items SET sort_order = -1 WHERE id = :i"), {"i": item.id})
        _db.session.commit()
    _db.session.rollback()


def test_admin_sets_order_through_the_forms(app, db):
    client, _ = signed_in(app, "boss", Role.ADMIN)
    req(client, "post", "/admin/categories/create", data={"name": "Mains", "is_active": "y", "sort_order": "7"})
    category = _db.session.query(Category).one()
    assert category.sort_order == 7
    req(client, "post", "/admin/menu/create", data={
        "category_id": category.id, "name": "Momo", "price": "150", "cuisine": "indian", "spice_level": "mild",
        "dietary_type": "vegetarian", "is_available": "y", "sort_order": "3"})
    assert _db.session.query(MenuItem).one().sort_order == 3
    response = req(client, "post", "/admin/categories/create", data={"name": "Bad", "sort_order": "-4"})
    assert response.status_code == 200 and _db.session.query(Category).count() == 1


# =============================================================================
# bilingual content
# =============================================================================


def test_localized_prefers_the_requested_translation(db):
    item = make_menu_item(name="Paneer Tikka")
    item.description = "Spiced paneer."
    add_translation(item)
    assert localized(item, "ne") == (NEPALI_NAME, NEPALI_DESC)
    assert localized(item, "en") == ("Paneer Tikka", "Spiced paneer.")  # no English row: canonical


def test_english_translation_is_used_when_present(db):
    item = make_menu_item(name="Paneer Tikka")
    add_translation(item, "en", "Cottage Cheese Tikka", None)
    assert localized(item, "en").name == "Cottage Cheese Tikka"


def test_missing_nepali_falls_back_to_canonical(db):
    item = make_menu_item(name="Paneer Tikka")
    item.description = "Spiced paneer."
    _db.session.commit()
    assert localized(item, "ne") == ("Paneer Tikka", "Spiced paneer.")


def test_translation_without_description_keeps_the_canonical_description(db):
    item = make_menu_item(name="Paneer Tikka")
    item.description = "Spiced paneer."
    add_translation(item, description=None)
    assert localized(item, "ne") == (NEPALI_NAME, "Spiced paneer.")


def test_category_translation(db):
    category = make_category(name="Starters")
    add_translation(category, name=NEPALI_CATEGORY, description=None)
    assert localized(category, "ne").name == NEPALI_CATEGORY
    assert localized(category, "en").name == "Starters"


def test_one_translation_per_locale(db):
    item = make_menu_item()
    add_translation(item)
    _db.session.add(MenuItemTranslation(menu_item_id=item.id, locale=Locale.NE, name="दोस्रो"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_unsupported_locale_is_refused_by_database_and_service(db):
    item = make_menu_item()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("INSERT INTO menu_item_translations (menu_item_id, locale, name, created_at,"
                                    " updated_at) VALUES (:i, 'fr', 'Tikka', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                            {"i": item.id})
        _db.session.commit()
    _db.session.rollback()
    with pytest.raises(MenuItemError) as exc:
        update_menu_item(item, MenuItemInput(item.category_id, item.name, None, "199", "indian", "medium",
                                             "vegetarian", translations={"fr": ("Tikka", None)}))
    assert "translations" in exc.value.errors


def test_translation_needs_an_existing_parent(db):
    _db.session.add(MenuItemTranslation(menu_item_id=987654, locale=Locale.NE, name=NEPALI_NAME))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_translations_are_removed_with_their_category(db):
    category = make_category()
    add_translation(category, name=NEPALI_CATEGORY)
    _db.session.delete(category)
    _db.session.commit()
    assert _db.session.query(CategoryTranslation).count() == 0


def test_devanagari_round_trip_insert_read_update(db):
    """On MySQL this proves utf8mb4 end to end, not just Python strings."""
    item = make_menu_item()
    add_translation(item)
    _db.session.expire_all()
    stored = _db.session.query(MenuItemTranslation).one()
    assert (stored.name, stored.description) == (NEPALI_NAME, NEPALI_DESC)
    stored.name = "मःम"
    _db.session.commit()
    _db.session.expire_all()
    raw = _db.session.execute(sa.text("SELECT name FROM menu_item_translations")).scalar()
    assert raw == "मःम"


def test_admin_enters_edits_and_clears_nepali_text(app, db):
    client, _ = signed_in(app, "boss", Role.ADMIN)
    category = make_category()
    form = {"category_id": category.id, "name": "Paneer Tikka", "description": "Spiced paneer.", "price": "199",
            "cuisine": "indian", "spice_level": "medium", "dietary_type": "vegetarian", "is_available": "y",
            "name_ne": NEPALI_NAME, "description_ne": NEPALI_DESC}
    req(client, "post", "/admin/menu/create", data=form)
    item = _db.session.query(MenuItem).one()
    assert localized(item, "ne") == (NEPALI_NAME, NEPALI_DESC)
    assert NEPALI_NAME in body(req(client, "get", f"/admin/menu/{item.id}/edit"))  # pre-filled

    req(client, "post", f"/admin/menu/{item.id}/edit", data={**form, "name_ne": "मःम"})
    _db.session.expire_all()
    assert localized(item, "ne").name == "मःम"
    assert _db.session.query(MenuItemTranslation).count() == 1  # updated in place

    without_nepali = {k: v for k, v in form.items() if not k.endswith("_ne")}
    req(client, "post", f"/admin/menu/{item.id}/edit", data=without_nepali)  # fields absent: kept
    _db.session.expire_all()
    assert localized(item, "ne").name == "मःम"

    req(client, "post", f"/admin/menu/{item.id}/edit", data={**form, "name_ne": "", "description_ne": ""})
    _db.session.expire_all()
    assert _db.session.query(MenuItemTranslation).count() == 0
    assert localized(item, "ne").name == "Paneer Tikka"


def test_editing_nepali_leaves_other_locales_alone(db):
    item = make_menu_item(name="Paneer Tikka")
    add_translation(item, "en", "Cottage Cheese Tikka", None)
    base = MenuItemInput(item.category_id, item.name, None, "199", "indian", "medium", "vegetarian")
    base.translations = {"ne": (NEPALI_NAME, None)}
    update_menu_item(item, base)
    base.translations = {"ne": ("", "")}
    update_menu_item(item, base)
    _db.session.expire_all()
    assert [(t.locale.value, t.name) for t in _db.session.query(MenuItemTranslation)] == [
        ("en", "Cottage Cheese Tikka")]


def test_nepali_description_without_a_name_is_refused(app, db):
    client, _ = signed_in(app, "boss", Role.ADMIN)
    category = make_category()
    req(client, "post", "/admin/menu/create", data={
        "category_id": category.id, "name": "Momo", "price": "150", "cuisine": "indian", "spice_level": "mild",
        "dietary_type": "vegetarian", "description_ne": NEPALI_DESC})
    assert _db.session.query(MenuItem).count() == 0


def test_admin_enters_a_category_translation(app, db):
    client, _ = signed_in(app, "boss", Role.ADMIN)
    req(client, "post", "/admin/categories/create", data={"name": "Starters", "is_active": "y",
                                                         "name_ne": NEPALI_CATEGORY})
    assert localized(_db.session.query(Category).one(), "ne").name == NEPALI_CATEGORY


def test_translated_content_is_escaped(client, db):
    item = make_menu_item()
    add_translation(item, name="<script>alert('ne')</script>", description="<b>bold</b>")
    page = body(req(client, "get", "/menu?lang=ne"))
    assert "<script>alert('ne')</script>" not in page and "<b>bold</b>" not in page
    assert "&lt;script&gt;" in page


# =============================================================================
# locale
# =============================================================================


def html_lang(page: str) -> str:
    start = page.index('<html lang="') + len('<html lang="')
    return page[start:page.index('"', start)]


def test_default_locale_is_english(client, db):
    assert html_lang(body(req(client, "get", "/menu"))) == "en"


def test_restaurant_default_locale_applies_without_a_preference(client, db):
    seed_profile(default_locale=Locale.NE)
    assert html_lang(body(req(client, "get", "/menu"))) == "ne"


@pytest.mark.parametrize("header, expected", [("ne-NP,ne;q=0.9,en;q=0.5", "ne"), ("ne", "ne"),
                                              ("en-US,en;q=0.9", "en"), ("fr-FR,de;q=0.8", "ne")])
def test_browser_language_comes_before_the_restaurant_default(client, db, header, expected):
    seed_profile(default_locale=Locale.NE)
    page = body(req(client, "get", "/menu", headers={"Accept-Language": header}))
    assert html_lang(page) == expected


def test_lang_param_switches_and_is_remembered(client, db):
    item = make_menu_item(name="Paneer Tikka")
    add_translation(item)
    page = body(req(client, "get", "/menu?lang=ne"))
    assert html_lang(page) == "ne" and NEPALI_NAME in page
    page = body(req(client, "get", "/menu", headers={"Accept-Language": "en"}))
    assert html_lang(page) == "ne"  # the session choice beats the browser
    page = body(req(client, "get", "/menu?lang=en"))
    assert html_lang(page) == "en" and NEPALI_NAME not in page and "Paneer Tikka" in page


@pytest.mark.parametrize("value", ["fr", "NE-np", "", "<script>alert(1)</script>", "ne\x00"])
def test_invalid_lang_param_is_ignored_and_never_reflected(client, db, value):
    page = body(req(client, "get", "/menu", query_string={"lang": value}))
    assert html_lang(page) == "en" and "<script>alert(1)" not in page


def test_switcher_sets_the_locale_and_returns_to_a_local_page(client, db):
    response = req(client, "post", "/locale", data={"lang": "ne", "next": "/menu/5"})
    assert response.status_code == 303 and response.headers["Location"].endswith("/menu/5")
    assert html_lang(body(req(client, "get", "/"))) == "ne"


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/x", "/\\evil.example",
                                    "javascript:alert(1)", "http:/evil.example", "menu", "/ok\r\nSet-Cookie: x=1",
                                    "\\\\evil.example"])
def test_switcher_never_redirects_off_site(client, db, target):
    response = req(client, "post", "/locale", data={"lang": "ne", "next": target})
    assert response.status_code == 303 and response.headers["Location"].endswith("/menu")


def test_safe_next_accepts_local_paths_with_queries():
    assert safe_next("/menu?category_id=3", "/x") == "/menu?category_id=3"
    assert safe_next(None, "/x") == "/x"


@pytest.mark.parametrize("value", ["fr", "", "english", None])
def test_switcher_refuses_unsupported_locales(client, db, value):
    data = {"next": "/menu"} if value is None else {"lang": value, "next": "/menu"}
    assert req(client, "post", "/locale", data=data).status_code == 400
    assert html_lang(body(req(client, "get", "/menu"))) == "en"


def test_switcher_requires_csrf_and_get_cannot_use_it(app, client, db):
    assert req(client, "get", "/locale").status_code == 405
    app.config["WTF_CSRF_ENABLED"] = True
    assert req(client, "post", "/locale", data={"lang": "ne", "next": "/menu"}).status_code == 400
    assert html_lang(body(req(client, "get", "/menu"))) == "en"


def test_switcher_is_on_customer_pages_only(app, client, db):
    page = body(req(client, "get", "/menu"))
    assert 'action="/locale"' in page and 'value="ne"' in page and "नेपाली" in page
    staff_client, _ = signed_in(app, "cook", Role.STAFF)
    ops = body(req(staff_client, "get", "/staff/kitchen"))
    assert 'action="/locale"' not in ops


def test_operations_shell_stays_english(app, db):
    staff_client, _ = signed_in(app, "cook", Role.STAFF)
    assert html_lang(body(req(staff_client, "get", "/staff/kitchen?lang=ne"))) == "en"


def test_chosen_language_survives_sign_in_and_sign_out(app, client, db):
    make_user(username="cust", email="cust@example.com", password=PASSWORD)
    req(client, "get", "/menu?lang=ne")
    req(client, "post", "/login", data={"username": "cust", "password": PASSWORD})
    assert html_lang(body(req(client, "get", "/menu"))) == "ne"
    req(client, "post", "/logout")
    assert html_lang(body(req(client, "get", "/menu"))) == "ne"


def test_nepali_menu_renders_translated_items_and_categories(client, db):
    item = make_menu_item(name="Paneer Tikka")
    add_translation(item)
    add_translation(item.category, name=NEPALI_CATEGORY, description=None)
    menu = body(req(client, "get", "/menu?lang=ne"))
    assert NEPALI_NAME in menu and NEPALI_DESC in menu and NEPALI_CATEGORY in menu
    detail = body(req(client, "get", f"/menu/{item.id}"))
    assert f"<h1 class=\"display-6 mb-2\">{NEPALI_NAME}</h1>" in detail
    assert "paneer tikka" in menu  # canonical stays in the (lower-cased) search text


def test_nepali_font_is_vendored_and_declared(client):
    font = req(client, "get", "/static/fonts/noto-sans-devanagari-devanagari-wght-normal.woff2")
    assert font.status_code == 200 and font.data[:4] == b"wOF2"
    css = body(req(client, "get", "/static/css/app.css"))
    assert 'font-family: "Noto Sans Devanagari"' in css and "U+0900-097F" in css
    assert '"Inter", "Noto Sans Devanagari"' in css


# =============================================================================
# staff sold-out control
# =============================================================================


def set_avail(client, item_id, available, **extra):
    return req(client, "post", f"/staff/menu/{item_id}/availability", data={"available": available, **extra})


def fresh(item) -> MenuItem:
    _db.session.expire_all()
    return _db.session.get(MenuItem, item.id)


@pytest.mark.parametrize("role", [Role.STAFF, Role.ADMIN])
def test_staff_and_admin_mark_sold_out_and_available(app, db, role):
    item = make_menu_item()
    client, actor = signed_in(app, "floor", role)
    assert req(client, "get", "/staff/menu").status_code == 200
    assert set_avail(client, item.id, "0").status_code == 302
    assert fresh(item).is_available is False
    assert set_avail(client, item.id, "1").status_code == 302
    assert fresh(item).is_available is True
    rows = audit(AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED)
    assert [json.loads(r.metadata_json)["is_available"] for r in rows] == [False, True]
    assert {r.user_id for r in rows} == {actor.id}
    assert json.loads(rows[0].metadata_json)["menu_item_id"] == item.id


@pytest.mark.parametrize("role, status", [(Role.CUSTOMER, 403), (None, 401)])
def test_customers_and_anonymous_cannot_change_availability(app, db, role, status):
    item = make_menu_item()
    client = signed_in(app, "someone", role)[0] if role else app.test_client()
    assert req(client, "get", "/staff/menu").status_code == status
    assert set_avail(client, item.id, "0").status_code == status
    assert fresh(item).is_available is True
    assert audit(AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED) == []


def test_availability_cannot_be_changed_with_get(app, db):
    item = make_menu_item()
    client, _ = signed_in(app, "cook", Role.STAFF)
    assert req(client, "get", f"/staff/menu/{item.id}/availability?available=0").status_code == 405
    assert fresh(item).is_available is True


def test_availability_requires_csrf(app, db):
    item = make_menu_item()
    client, _ = signed_in(app, "cook", Role.STAFF)
    app.config["WTF_CSRF_ENABLED"] = True
    assert set_avail(client, item.id, "0").status_code == 400
    assert fresh(item).is_available is True


@pytest.mark.parametrize("value", ["", "yes", "2", "true"])
def test_availability_needs_an_explicit_state(app, db, value):
    item = make_menu_item()
    client, _ = signed_in(app, "cook", Role.STAFF)
    assert set_avail(client, item.id, value).status_code == 400
    assert fresh(item).is_available is True


def test_unknown_item_is_404(app, db):
    client, _ = signed_in(app, "cook", Role.STAFF)
    assert set_avail(client, 424242, "0").status_code == 404


def test_availability_route_changes_nothing_else(app, db):
    item = make_menu_item(name="Paneer Tikka", price="199.00")
    add_translation(item)
    client, _ = signed_in(app, "cook", Role.STAFF)
    other = make_category(name="Other")
    set_avail(client, item.id, "0", price="1.00", name="Hacked", category_id=other.id, sort_order="99",
              name_ne="x", description="x", is_available="1")
    item = fresh(item)
    assert (item.is_available, item.name, item.price, item.category_id, item.sort_order) == (
        False, "Paneer Tikka", Decimal("199.00"), item.category_id, 0)
    assert item.category.name != "Other" and localized(item, "ne").name == NEPALI_NAME


def test_staff_still_cannot_use_the_admin_edit_form(app, db):
    item = make_menu_item()
    client, _ = signed_in(app, "cook", Role.STAFF)
    assert req(client, "post", f"/admin/menu/{item.id}/edit", data={"is_available": ""}).status_code == 403


def test_repeating_a_state_writes_no_second_audit_row(app, db):
    item = make_menu_item()
    client, _ = signed_in(app, "cook", Role.STAFF)
    set_avail(client, item.id, "0")
    set_avail(client, item.id, "0")
    assert len(audit(AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED)) == 1


def test_availability_service_refuses_customers(db):
    item = make_menu_item()
    customer = make_user(username="cust", email="cust@example.com")
    with pytest.raises(MenuItemError):
        set_availability(customer, item.id, False)
    assert fresh(item).is_available is True


def test_availability_change_rolls_back_when_audit_fails(app, db, monkeypatch):
    item = make_menu_item()
    client, _ = signed_in(app, "cook", Role.STAFF)

    def boom(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr("app.routes.staff_menu.record_event", boom)
    assert set_avail(client, item.id, "0").status_code == 500
    assert fresh(item).is_available is True


def test_staff_nav_links_to_menu_availability(app, db):
    client, _ = signed_in(app, "cook", Role.STAFF)
    page = body(req(client, "get", "/staff/kitchen"))
    assert 'href="/staff/menu"' in page and 'href="/admin/' not in page


# =============================================================================
# one public-menu rule
# =============================================================================


def test_sold_out_item_leaves_the_menu_cart_and_checkout(app, db):
    item = make_menu_item(name="Paneer Tikka")
    customer = make_user(username="cust", email="cust@example.com", password=PASSWORD)
    staff_client, _ = signed_in(app, "cook", Role.STAFF)
    set_avail(staff_client, item.id, "0")

    assert get_public_menu_item(item.id) is None and get_available_menu_item(item.id) is None
    assert list_public_menu_items() == []
    client = app.test_client()
    req(client, "post", "/login", data={"username": "cust", "password": PASSWORD})
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    assert "Paneer Tikka" not in body(req(client, "get", "/cart"))
    with pytest.raises(CheckoutError):
        checkout(customer.id, {str(item.id): 1})


def test_inactive_category_hides_its_items_everywhere(db):
    item = make_menu_item(category=make_category(name="Hidden", is_active=False))
    assert get_public_menu_item(item.id) is None
    assert get_available_menu_item(item.id) is None
    assert list_public_menu_items() == [] and list_public_categories() == []


def test_available_item_in_active_category_is_public_everywhere(db):
    item = make_menu_item()
    assert get_public_menu_item(item.id) == item == get_available_menu_item(item.id)
    assert list_public_menu_items() == [item]


# =============================================================================
# canonical text stays canonical
# =============================================================================


def test_order_snapshot_is_canonical_and_never_rewritten(app, db):
    item = make_menu_item(name="Paneer Tikka")
    customer = make_user(username="cust", email="cust@example.com")
    order = checkout(customer.id, {str(item.id): 1})
    add_translation(item)
    _db.session.expire_all()
    line = _db.session.query(OrderItem).filter_by(order_id=order.id).one()
    assert line.item_name_snapshot == "Paneer Tikka"

    translation = _db.session.query(MenuItemTranslation).one()
    translation.name = "नयाँ नाम"
    _db.session.commit()
    _db.session.expire_all()
    assert _db.session.query(OrderItem).filter_by(order_id=order.id).one().item_name_snapshot == "Paneer Tikka"

    # A Nepali-locale order still snapshots the canonical name.
    client = app.test_client()
    req(client, "get", "/menu?lang=ne")
    second = checkout(customer.id, {str(item.id): 1})
    assert second.items[0].item_name_snapshot == "Paneer Tikka"


def test_kitchen_and_recommendations_use_canonical_text(app, db):
    item = make_menu_item(name="Paneer Tikka")
    add_translation(item)
    document = build_item_document(item)
    assert NEPALI_NAME not in document and "paneer tikka" in document
    customer = make_user(username="cust", email="cust@example.com")
    checkout(customer.id, {str(item.id): 1})
    client, _ = signed_in(app, "cook", Role.STAFF)
    kitchen = body(req(client, "get", "/staff/kitchen?lang=ne"))
    assert "Paneer Tikka" in kitchen and NEPALI_NAME not in kitchen


# =============================================================================
# query count
# =============================================================================


def count_queries(fn) -> int:
    statements = []

    def record(*_args):
        statements.append(1)

    sa.event.listen(_db.engine, "before_cursor_execute", record)
    try:
        fn()
    finally:
        sa.event.remove(_db.engine, "before_cursor_execute", record)
    return len(statements)


def menu_of(size: int) -> None:
    for c in range(3):
        category = make_category(name=f"Cat {size}-{c}")
        add_translation(category, name=f"श्रेणी {c}")
        for i in range(size):
            item = make_menu_item(category=category, name=f"Dish {size}-{c}-{i}")
            add_translation(item, name=f"परिकार {i}")


def test_translations_do_not_add_a_query_per_item(client, db):
    menu_of(2)
    small = count_queries(lambda: req(client, "get", "/menu?lang=ne"))
    menu_of(8)
    large = count_queries(lambda: req(client, "get", "/menu?lang=ne"))
    assert large == small, (small, large)
