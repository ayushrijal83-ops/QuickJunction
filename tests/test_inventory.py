"""Milestone 13: inventory -- ingredients as stock, recipes, the movement
ledger, sale deduction on completion, and the authorization around it."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import sqlalchemy as sa
from flask import g
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.ingredient import Ingredient, StockUnit
from app.models.menu_item_ingredient import MenuItemIngredient
from app.models.order import Order, OrderStatus
from app.models.stock_movement import MovementType, StockMovement
from app.models.user import Role
from app.services.inventory import (
    InventoryError,
    add_to_recipe,
    deduct_for_order,
    record_movement,
    save_ingredient,
    set_recipe,
    shortages,
)
from app.services.menu import sync_menu_item_ingredients
from app.services.orders import CheckoutError, OrderStatusError, checkout, update_order_status
from tests.conftest import csrf_token, make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"
D = Decimal


def member(name="cook", role=Role.STAFF):
    return make_user(username=name, email=f"{name}@example.com", password=PASSWORD, role=role)


def login(client, name):
    g.pop("current_user", None)  # known issue #29
    client.post("/login", data={"username": name, "password": PASSWORD})


def audit(event):
    return [json.loads(r.metadata_json) for r in _db.session.query(AuditLog).filter_by(event_type=event)]


def movements(ingredient_id=None):
    q = _db.session.query(StockMovement)
    return q.filter_by(ingredient_id=ingredient_id).all() if ingredient_id else q.all()


@pytest.fixture
def kitchen(db):
    """A burger with a real recipe, stocked by a PURCHASE each."""
    boss = member("boss", Role.ADMIN)
    burger = make_menu_item(category=make_category(name="Mains"), name="Burger", price="300.00")
    stock = {}
    for name, unit, qty, per in (("bun", "piece", "10", "1"), ("patty", "piece", "10", "1"), ("sauce", "ml", "500", "20")):
        ingredient = save_ingredient(None, name, unit, "3" if unit == "piece" else "100", True)
        record_movement(ingredient, "purchase", qty, "opening stock", boss)
        add_to_recipe(burger, ingredient, per)
        stock[name] = ingredient
    return {"boss": boss, "burger": burger, **stock}


def buy(kitchen, qty=2, username="diner"):
    user = make_user(username=username, email=f"{username}@example.com", password=PASSWORD)
    return checkout(user.id, {str(kitchen["burger"].id): qty})


def complete(order, actor=None):
    for status in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.COMPLETED):
        update_order_status(order, status, actor=actor)


def qty(ingredient):
    _db.session.expire_all()
    return _db.session.get(Ingredient, ingredient.id).current_quantity


# --- ingredients -------------------------------------------------------------------------


def test_ingredient_creation_starts_at_zero_and_validates(db):
    flour = save_ingredient(None, "  Flour ", "kg", "2.5", True)
    assert (flour.name, flour.unit, flour.current_quantity, flour.minimum_quantity) == ("flour", StockUnit.KILOGRAM, D("0"), D("2.500"))
    assert movements() == []  # a definition never moves stock
    for bad in (("flour", "kg", "1"), ("", "kg", "1"), ("rice", "bucket", "1"), ("rice", "kg", "-1"), ("rice", "kg", "x")):
        with pytest.raises(InventoryError):
            save_ingredient(None, *bad, True)


# --- movements -------------------------------------------------------------------------


def test_purchase_restock_waste_adjustment_are_ledgered(db):
    cook, boss = member(), member("boss", Role.ADMIN)
    rice = save_ingredient(None, "rice", "kg", "5", True)
    record_movement(rice, "purchase", "20", "supplier A", cook)
    record_movement(rice, "restock", "2.5", None, cook)
    record_movement(rice, "waste", "1.25", "spilled", cook)
    record_movement(rice, "adjustment", "-0.25", "stock count", boss)
    assert qty(rice) == D("21.000")
    rows = sorted(movements(rice.id), key=lambda m: m.id)
    assert [(m.movement_type, m.quantity_change, m.quantity_after) for m in rows] == [
        (MovementType.PURCHASE, D("20.000"), D("20.000")),
        (MovementType.RESTOCK, D("2.500"), D("22.500")),
        (MovementType.WASTE, D("-1.250"), D("21.250")),
        (MovementType.ADJUSTMENT, D("-0.250"), D("21.000")),
    ]
    assert rows[0].actor_id == cook.id and rows[3].actor_id == boss.id


@pytest.mark.parametrize("mtype, amount, note", [
    ("purchase", "0", None), ("purchase", "-5", None), ("purchase", "abc", None), ("purchase", "NaN", None),
    ("purchase", "10000001", None), ("waste", "1", ""), ("adjustment", "1", ""), ("sale", "1", "x"), ("bogus", "1", "x"),
])
def test_invalid_movements_rejected(db, mtype, amount, note):
    rice = save_ingredient(None, "rice", "kg", "5", True)
    with pytest.raises(InventoryError):
        record_movement(rice, mtype, amount, note, member("boss", Role.ADMIN))
    assert qty(rice) == D("0") and movements() == []


def test_staff_cannot_adjust(db):
    rice = save_ingredient(None, "rice", "kg", "5", True)
    with pytest.raises(InventoryError):
        record_movement(rice, "adjustment", "5", "count", member())


# --- recipes ---------------------------------------------------------------------------


def test_recipe_quantities_survive_menu_form_edits(kitchen):
    burger = kitchen["burger"]
    sync_menu_item_ingredients(burger, ["bun", "patty", "sauce", "lettuce"])  # the menu form's list
    _db.session.commit()
    rows = {r.ingredient.name: r.quantity for r in _db.session.query(MenuItemIngredient).filter_by(menu_item_id=burger.id)}
    assert rows == {"bun": D("1.000"), "patty": D("1.000"), "sauce": D("20.000"), "lettuce": D("0.000")}
    set_recipe(burger, {kitchen["sauce"].id: "25"})
    with pytest.raises(InventoryError):
        set_recipe(burger, {999999: "1"})
    with pytest.raises(InventoryError):
        set_recipe(burger, {kitchen["bun"].id: "-1"})


# --- sale deduction ----------------------------------------------------------------------


def test_completion_deducts_the_recipe_exactly_once(kitchen):
    cook = member()
    order = buy(kitchen, qty=2)
    update_order_status(order, OrderStatus.CONFIRMED)
    assert movements(kitchen["bun"].id)[-1].movement_type == MovementType.PURCHASE  # not deducted before completion
    complete_from = [OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.COMPLETED]
    for status in complete_from:
        update_order_status(order, status, actor=cook)
    assert (qty(kitchen["bun"]), qty(kitchen["patty"]), qty(kitchen["sauce"])) == (D("8"), D("8"), D("460"))
    sales = [m for m in movements() if m.movement_type == MovementType.SALE]
    assert {(m.ingredient_id, m.quantity_change, m.order_id, m.actor_id) for m in sales} == {
        (kitchen["bun"].id, D("-2.000"), order.id, cook.id),
        (kitchen["patty"].id, D("-2.000"), order.id, cook.id),
        (kitchen["sauce"].id, D("-40.000"), order.id, cook.id),
    }
    # retried processing: nothing more
    assert deduct_for_order(order.id, cook.id) == 0
    _db.session.commit()
    assert len([m for m in movements() if m.movement_type == MovementType.SALE]) == 3
    assert qty(kitchen["bun"]) == D("8")


def test_database_rejects_a_second_sale_row(kitchen):
    order = buy(kitchen)
    complete(order)
    with pytest.raises(sa.exc.IntegrityError):
        _db.session.add(StockMovement(ingredient_id=kitchen["bun"].id, movement_type=MovementType.SALE,
                                      quantity_change=D("-1"), quantity_after=D("0"), order_id=order.id))
        _db.session.flush()
    _db.session.rollback()


def test_cancelled_and_unfinished_orders_deduct_nothing(kitchen):
    cancelled, pending = buy(kitchen, username="a"), buy(kitchen, username="b")
    update_order_status(cancelled, OrderStatus.CANCELLED)
    assert [m for m in movements() if m.movement_type == MovementType.SALE] == []
    assert qty(kitchen["bun"]) == D("10")
    assert pending.status == OrderStatus.PENDING


def test_untracked_ingredients_are_not_deducted(kitchen):
    kitchen["sauce"].is_active = False
    _db.session.commit()
    complete(buy(kitchen))
    assert qty(kitchen["sauce"]) == D("500") and qty(kitchen["bun"]) == D("8")


def test_failed_completion_rolls_back_the_deduction(kitchen, monkeypatch):
    import app.services.orders as orders_service

    real = orders_service.deduct_for_order

    def deduct_then_fail(order_id, actor_id):
        real(order_id, actor_id)
        raise SQLAlchemyError("simulated failure after deducting")

    order = buy(kitchen)
    for status in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
        update_order_status(order, status)
    monkeypatch.setattr(orders_service, "deduct_for_order", deduct_then_fail)
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.COMPLETED)
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).status == OrderStatus.READY
    assert qty(kitchen["bun"]) == D("10")
    assert [m for m in movements() if m.movement_type == MovementType.SALE] == []


# --- insufficient stock --------------------------------------------------------------------


def test_checkout_refuses_what_cannot_be_made(kitchen):
    assert shortages({kitchen["burger"].id: 10}) == []
    assert shortages({kitchen["burger"].id: 11}) == ["bun", "patty"]
    with pytest.raises(CheckoutError) as exc:
        buy(kitchen, qty=11)
    assert "out of bun, patty" in exc.value.errors["cart"][0]
    assert _db.session.query(Order).count() == 0


def test_served_order_completes_even_if_stock_went_short(kitchen):
    """Stock is checked at checkout, not reserved: two orders can pass and
    the second completion takes stock negative -- shown as short, never
    blocking an order that has already been served."""
    first, second = buy(kitchen, qty=6, username="a"), buy(kitchen, qty=6, username="b")
    complete(first)
    complete(second)
    assert second.status == OrderStatus.COMPLETED
    bun = _db.session.get(Ingredient, kitchen["bun"].id)
    assert bun.current_quantity == D("-2") and bun.is_low


def test_low_stock_detection(kitchen):
    bun = kitchen["bun"]
    assert not bun.is_low
    complete(buy(kitchen, qty=7))  # 10 - 7 = 3 = minimum
    _db.session.refresh(bun)
    assert bun.is_low
    zero_min = save_ingredient(None, "salt", "g", "0", True)
    assert not zero_min.is_low  # nothing set to warn about


# --- routes: authorization, audit, CSRF -------------------------------------------------------


def test_inventory_rbac(app, client, kitchen):
    bun = kitchen["bun"]
    assert client.get("/staff/inventory").status_code == 401
    make_user(username="diner", email="diner@example.com", password=PASSWORD)
    login(client, "diner")
    for method, url in (("get", "/staff/inventory"), ("get", f"/staff/inventory/{bun.id}"),
                        ("post", f"/staff/inventory/{bun.id}/movement"), ("get", "/admin/inventory/new"),
                        ("get", f"/admin/menu/{kitchen['burger'].id}/recipe")):
        assert getattr(client, method)(url).status_code == 403, url

    staff = app.test_client()
    member()
    login(staff, "cook")
    assert staff.get("/staff/inventory").status_code == 200
    assert staff.get(f"/staff/inventory/{bun.id}").status_code == 200
    assert staff.get("/admin/inventory/new").status_code == 403
    assert staff.post(f"/admin/menu/{kitchen['burger'].id}/recipe", data={f"q_{bun.id}": "99"}).status_code == 403
    staff.post(f"/staff/inventory/{bun.id}/movement", data={"movement_type": "adjustment", "quantity": "100", "note": "x"})
    assert qty(bun) == D("10")  # staff adjustment refused
    staff.post(f"/staff/inventory/{bun.id}/movement", data={"movement_type": "purchase", "quantity": "5"})
    assert qty(bun) == D("15")
    assert audit(AuditEvent.STOCK_MOVEMENT_RECORDED)[-1] == {
        "ingredient_id": bun.id, "movement_id": movements(bun.id)[-1].id, "type": "purchase",
        "change": "5.000", "after": "15.000"}


def test_admin_manages_ingredients_and_recipes(app, client, kitchen):
    login(client, "boss")
    client.post("/admin/inventory/new", data={"name": "Cheese", "unit": "piece", "minimum_quantity": "4", "is_active": "y"})
    cheese = _db.session.query(Ingredient).filter_by(name="cheese").one()
    assert audit(AuditEvent.STOCK_ITEM_SAVED)[-1] == {"ingredient_id": cheese.id, "created": True}
    burger = kitchen["burger"]
    client.post(f"/admin/menu/{burger.id}/recipe",
                data={f"q_{kitchen['sauce'].id}": "30", "add_ingredient_id": str(cheese.id), "add_quantity": "1"})
    recipe = {r.ingredient_id: r.quantity for r in _db.session.query(MenuItemIngredient).filter_by(menu_item_id=burger.id)}
    assert recipe[kitchen["sauce"].id] == D("30.000") and recipe[cheese.id] == D("1.000")
    assert audit(AuditEvent.RECIPE_UPDATED) == [{"menu_item_id": burger.id}]
    page = client.get("/staff/inventory").get_data(as_text=True)
    assert "cheese" in page and "Low stock" in page  # cheese: 0 in stock, low at 4


def test_movement_requires_csrf(app, client, kitchen):
    bun = kitchen["bun"]
    member()
    login(client, "cook")
    app.config["WTF_CSRF_ENABLED"] = True
    client.post(f"/staff/inventory/{bun.id}/movement", data={"movement_type": "purchase", "quantity": "5"})
    assert qty(bun) == D("10")
    token = csrf_token(client, f"/staff/inventory/{bun.id}")
    client.post(f"/staff/inventory/{bun.id}/movement",
                data={"movement_type": "purchase", "quantity": "5", "csrf_token": token})
    assert qty(bun) == D("15")


def test_customer_sees_out_of_stock_message_at_checkout(client, kitchen):
    make_user(username="diner", email="diner@example.com", password=PASSWORD)
    login(client, "diner")
    client.post("/cart/add", data={"menu_item_id": kitchen["burger"].id, "quantity": 11})
    page = client.post("/checkout", data={}, follow_redirects=True).get_data(as_text=True)
    assert "out of bun, patty" in page
    assert _db.session.query(Order).count() == 0
