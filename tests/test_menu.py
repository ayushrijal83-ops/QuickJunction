"""Milestone 03 tests: menu data layer, admin/staff/customer authorization,
validation, database security, audit logging, and public-route access.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.models.audit_log import AuditEvent, AuditLog
from app.models.category import Category
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.models.menu_item import MenuItem
from app.models.menu_item_ingredient import MenuItemIngredient
from app.models.user import Role
from app.services.menu import (
    CategoryError,
    CategoryInput,
    MenuItemError,
    MenuItemInput,
    create_category,
    create_menu_item,
    get_public_menu_item,
    list_public_menu_items,
    sync_menu_item_ingredients,
)
from tests.conftest import make_category, make_menu_item, make_user

ADMIN_PASSWORD = "correct-horse-1"


def login(client, username: str, password: str = ADMIN_PASSWORD):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def login_as_new(client, role, username="actor"):
    make_user(username=username, email=f"{username}@example.com", password=ADMIN_PASSWORD, role=role)
    login(client, username)


def valid_item_form(**overrides) -> dict:
    data = {
        "name": "Butter Chicken",
        "description": "Rich tomato gravy.",
        "price": "349.00",
        "cuisine": Cuisine.INDIAN.value,
        "spice_level": SpiceLevel.MEDIUM.value,
        "dietary_type": DietaryType.NON_VEGETARIAN.value,
        "is_available": "y",
        "ingredients": "chicken, butter, tomato",
    }
    data.update(overrides)
    return data


# --- Database (1-9) ---------------------------------------------------------


def test_1_category_creation(client, db):
    category = create_category(CategoryInput(name="Starters", description="Small plates", is_active=True))
    assert category.id is not None
    assert db.session.query(Category).filter_by(name="Starters").one().is_active is True


def test_2_menu_item_creation(client, db):
    category = make_category()
    item = create_menu_item(
        MenuItemInput(
            category_id=category.id,
            name="Samosa",
            description="Fried pastry.",
            price="99.00",
            cuisine=Cuisine.INDIAN.value,
            spice_level=SpiceLevel.MILD.value,
            dietary_type=DietaryType.VEGETARIAN.value,
            is_available=True,
        )
    )
    assert item.id is not None
    assert item.price == Decimal("99.00")


def test_3_category_relationship(client, db):
    category = make_category(name="Beverages")
    item = make_menu_item(category=category, name="Mango Lassi")
    assert item.category.id == category.id
    assert item in category.menu_items


def test_4_ingredient_relationship(client, db):
    item1 = make_menu_item(name="Paneer Tikka")
    item2 = make_menu_item(category=item1.category, name="Paneer Butter Masala")

    sync_menu_item_ingredients(item1, ["paneer", "yogurt"])
    sync_menu_item_ingredients(item2, ["paneer", "butter"])

    assert {i.name for i in item1.ingredients} == {"paneer", "yogurt"}
    # Same ingredient name reused as the same row, not duplicated.
    shared = [i for i in item1.ingredients if i.name == "paneer"][0]
    shared2 = [i for i in item2.ingredients if i.name == "paneer"][0]
    assert shared.id == shared2.id


def test_5_duplicate_category_rejected(client, db):
    create_category(CategoryInput(name="Starters", description=None, is_active=True))
    with pytest.raises(CategoryError):
        create_category(CategoryInput(name="Starters", description=None, is_active=True))


def test_6_foreign_key_behavior(client, db):
    category = make_category()
    item = make_menu_item(category=category)
    sync_menu_item_ingredients(item, ["salt"])

    # RESTRICT, proven at the database level: a raw Core DELETE bypasses
    # the ORM's own FK-nulling-on-delete behavior, so this only passes if
    # the database itself enforces the constraint (SQLite needs
    # `PRAGMA foreign_keys=ON`, set in app/extensions.py; MySQL/InnoDB
    # enforces it by default).
    with pytest.raises(IntegrityError):
        db.session.execute(delete(Category).where(Category.id == category.id))
        db.session.commit()
    db.session.rollback()
    assert db.session.query(Category.id).filter_by(id=category.id).first() is not None

    # CASCADE, same proof: raw Core DELETE of the menu item, then confirm
    # the database removed the association rows on its own.
    item_id = item.id
    db.session.execute(delete(MenuItem).where(MenuItem.id == item_id))
    db.session.commit()
    assert db.session.query(MenuItemIngredient).filter_by(menu_item_id=item_id).count() == 0


def test_7_invalid_enum_values_rejected(client, db):
    category = make_category()
    with pytest.raises(MenuItemError) as exc_info:
        create_menu_item(
            MenuItemInput(
                category_id=category.id,
                name="Mystery Dish",
                description=None,
                price="100.00",
                cuisine="klingon",
                spice_level=SpiceLevel.MILD.value,
                dietary_type=DietaryType.VEGETARIAN.value,
            )
        )
    assert "cuisine" in exc_info.value.errors


def test_7b_invalid_enum_value_rejected_at_the_database_too(client, db):
    """The service layer's allow-list check is one layer; the column's
    CHECK constraint (app/models/enums.py's enum_column) is the backstop.
    A raw SQL INSERT bypasses both the service *and* SQLAlchemy's own
    Python-side Enum validation, so this only passes if the database
    itself enforces the constraint."""
    category = make_category()
    with pytest.raises(IntegrityError):
        db.session.execute(
            sa.text(
                "INSERT INTO menu_items "
                "(category_id, name, price, cuisine, spice_level, dietary_type, is_available) "
                "VALUES (:cat, 'Raw Insert', 10.00, 'klingon', 'mild', 'vegetarian', 1)"
            ),
            {"cat": category.id},
        )
        db.session.commit()
    db.session.rollback()


def test_8_invalid_price_rejected(client, db):
    category = make_category()
    for bad_price in ("-5.00", "0", "not-a-number", "999999999.00"):
        with pytest.raises(MenuItemError):
            create_menu_item(
                MenuItemInput(
                    category_id=category.id,
                    name="Item",
                    description=None,
                    price=bad_price,
                    cuisine=Cuisine.INDIAN.value,
                    spice_level=SpiceLevel.MILD.value,
                    dietary_type=DietaryType.VEGETARIAN.value,
                )
            )


def test_9_decimal_price_preserved_correctly(client, db):
    category = make_category()
    item = create_menu_item(
        MenuItemInput(
            category_id=category.id,
            name="Tricky Price",
            description=None,
            price="10.10",
            cuisine=Cuisine.INDIAN.value,
            spice_level=SpiceLevel.MILD.value,
            dietary_type=DietaryType.VEGETARIAN.value,
        )
    )
    db.session.expire_all()
    reloaded = db.session.get(MenuItem, item.id)
    assert reloaded.price == Decimal("10.10")
    assert str(reloaded.price) == "10.10"  # no float drift (e.g. 10.099999999999998)


# --- Authorization (10-14) --------------------------------------------------


def test_10_customer_cannot_create_menu_items(client, db):
    login_as_new(client, Role.CUSTOMER)
    assert client.get("/admin/menu/create").status_code == 403
    assert client.post("/admin/menu/create", data=valid_item_form()).status_code == 403
    assert db.session.query(MenuItem).count() == 0


def test_11_customer_cannot_modify_menu_items(client, db):
    item = make_menu_item()
    login_as_new(client, Role.CUSTOMER)
    resp = client.post(f"/admin/menu/{item.id}/edit", data=valid_item_form(name="Hacked"))
    assert resp.status_code == 403
    db.session.refresh(item)
    assert item.name != "Hacked"


def test_12_staff_cannot_perform_admin_menu_actions(client, db):
    login_as_new(client, Role.STAFF)
    assert client.get("/admin/menu").status_code == 200  # staff CAN view
    assert client.get("/admin/menu/create").status_code == 403
    assert client.post("/admin/menu/create", data=valid_item_form()).status_code == 403
    assert client.get("/admin/categories").status_code == 403


def test_13_admin_can_create_menu_items(client, db):
    category = make_category()
    login_as_new(client, Role.ADMIN)
    resp = client.post("/admin/menu/create", data=valid_item_form(category_id=str(category.id)))
    assert resp.status_code == 302
    item = db.session.query(MenuItem).filter_by(name="Butter Chicken").one()
    assert item.price == Decimal("349.00")
    assert {i.name for i in item.ingredients} == {"chicken", "butter", "tomato"}


def test_14_admin_can_update_menu_items(client, db):
    item = make_menu_item(name="Old Name", price="100.00")
    login_as_new(client, Role.ADMIN)
    resp = client.post(
        f"/admin/menu/{item.id}/edit",
        data=valid_item_form(category_id=str(item.category_id), name="New Name", price="150.00"),
    )
    assert resp.status_code == 302
    db.session.refresh(item)
    assert item.name == "New Name"
    assert item.price == Decimal("150.00")


# --- Validation (15-17) -----------------------------------------------------


def test_15_invalid_input_rejected(client, db):
    login_as_new(client, Role.ADMIN)
    resp = client.post("/admin/categories/create", data={"name": "", "description": "x"})
    assert resp.status_code == 200  # re-rendered with errors, not redirected
    assert db.session.query(Category).filter_by(name="").first() is None


def test_16_missing_required_fields_rejected(client, db):
    category = make_category()
    login_as_new(client, Role.ADMIN)
    data = valid_item_form(category_id=str(category.id))
    del data["name"]
    resp = client.post("/admin/menu/create", data=data)
    assert resp.status_code == 200
    assert db.session.query(MenuItem).count() == 0


def test_17_client_price_manipulation_does_not_affect_trusted_price(client, db):
    item = make_menu_item(price="199.00")
    resp = client.get(f"/menu/{item.id}?price=1.00&price_override=0.01")
    body = resp.get_data(as_text=True)
    assert "199.00" in body
    assert "0.01" not in body  # spoofed override never read, never rendered
    db.session.refresh(item)
    assert item.price == Decimal("199.00")  # DB row untouched


# --- Security (18-21) -------------------------------------------------------


def test_18_csrf_required_for_state_changing_forms(app, client, db):
    login_as_new(client, Role.ADMIN)
    app.config["WTF_CSRF_ENABLED"] = True
    resp = client.post("/admin/categories/create", data={"name": "No Token", "description": ""})
    assert resp.status_code == 400
    assert db.session.query(Category).filter_by(name="No Token").first() is None


def test_19_sql_injection_style_input_is_treated_as_data(client, db):
    login_as_new(client, Role.ADMIN)
    malicious = "Drinks'; DROP TABLE categories; --"
    resp = client.post("/admin/categories/create", data={"name": malicious, "description": ""})
    assert resp.status_code == 302
    stored = db.session.query(Category).filter_by(name=malicious).one()
    assert stored.name == malicious  # stored literally, not executed
    # The table is still there and queryable.
    assert db.session.query(Category).count() >= 1


def test_20_xss_payloads_are_safely_handled(client, db):
    category = make_category()
    payload = "<script>alert(1)</script>"
    make_menu_item(category=category, name=payload)
    resp = client.get("/menu")
    body = resp.get_data(as_text=True)
    assert payload not in body  # never rendered unescaped
    assert "&lt;script&gt;" in body  # Jinja2 autoescaping did its job


def test_21_menu_route_errors_do_not_expose_internals(app, client, db, monkeypatch):
    import app.routes.menu as menu_routes

    def _boom(**kwargs):
        raise RuntimeError("should never reach the client")

    monkeypatch.setattr(menu_routes, "list_public_menu_items", _boom)
    resp = client.get("/menu")
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "RuntimeError" not in body
    assert "Traceback" not in body
    assert resp.get_json()["error"]["message"] == "An internal error occurred."


# --- Audit (22-23) -----------------------------------------------------------


def test_22_admin_menu_changes_create_audit_events(client, db):
    category = make_category()
    login_as_new(client, Role.ADMIN)

    client.post("/admin/categories/create", data={"name": "New Cat", "description": ""})
    assert db.session.query(AuditLog).filter_by(event_type=AuditEvent.CATEGORY_CREATED).count() == 1

    resp = client.post("/admin/menu/create", data=valid_item_form(category_id=str(category.id)))
    assert resp.status_code == 302
    item = db.session.query(MenuItem).filter_by(name="Butter Chicken").one()
    assert db.session.query(AuditLog).filter_by(event_type=AuditEvent.MENU_ITEM_CREATED).count() == 1
    assert db.session.query(AuditLog).filter_by(event_type=AuditEvent.INGREDIENT_CHANGED).count() == 1

    # Flip availability only -> a dedicated availability-changed event.
    client.post(
        f"/admin/menu/{item.id}/edit",
        data=valid_item_form(category_id=str(item.category_id), is_available=""),
    )
    availability_events = db.session.query(AuditLog).filter_by(
        event_type=AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED
    ).all()
    assert len(availability_events) == 1


def test_23_sensitive_information_absent_from_menu_audit_logs(client, db):
    category = make_category()
    login_as_new(client, Role.ADMIN, username="menu_admin")
    client.post("/admin/menu/create", data=valid_item_form(category_id=str(category.id)))

    entries = db.session.query(AuditLog).all()
    assert entries
    for entry in entries:
        blob = f"{entry.metadata_json} {entry.user_agent}"
        assert ADMIN_PASSWORD not in blob
        assert "password" not in blob.lower()
        assert "argon2" not in blob.lower()


# --- Access (24-25) -----------------------------------------------------------


def test_24_unauthenticated_access_is_correct(client, db):
    item = make_menu_item()
    assert client.get("/menu").status_code == 200
    assert client.get(f"/menu/{item.id}").status_code == 200
    assert client.get("/admin/menu").status_code == 401
    assert client.get("/admin/menu/create").status_code == 401
    assert client.get("/admin/categories").status_code == 401
    assert client.post("/admin/categories/create", data={"name": "x"}).status_code == 401


def test_25_inactive_items_not_exposed_publicly(client, db):
    active_category = make_category(name="Active Cat")
    unavailable_item = make_menu_item(category=active_category, name="Sold Out", is_available=False)

    inactive_category = make_category(name="Inactive Cat", is_active=False)
    hidden_item = make_menu_item(category=inactive_category, name="Hidden Category Item", is_available=True)

    visible_item = make_menu_item(category=active_category, name="Visible Item", is_available=True)

    items = list_public_menu_items()
    names = {i.name for i in items}
    assert "Sold Out" not in names
    assert "Hidden Category Item" not in names
    assert "Visible Item" in names

    assert get_public_menu_item(unavailable_item.id) is None
    assert get_public_menu_item(hidden_item.id) is None
    assert get_public_menu_item(visible_item.id) is not None

    resp = client.get("/menu")
    body = resp.get_data(as_text=True)
    assert "Sold Out" not in body
    assert "Hidden Category Item" not in body
    assert "Visible Item" in body

    assert client.get(f"/menu/{unavailable_item.id}").status_code == 404
    assert client.get(f"/menu/{hidden_item.id}").status_code == 404

