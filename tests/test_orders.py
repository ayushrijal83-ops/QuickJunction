"""Milestone 04 tests: cart, checkout, order placement, order history, the
server-side price/subtotal/total trust boundary, and IDOR protection.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order, OrderItem, OrderStatus
from app.models.user import Role
from app.services.cart import (
    MAX_QUANTITY_PER_ITEM,
    MIN_QUANTITY,
    CartError,
    add_item,
    build_cart_view,
    remove_item,
    update_item,
)
from app.services.orders import CheckoutError, checkout, get_order_for_user, list_orders_for_user
from tests.conftest import make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"


def login(client, username: str, password: str = PASSWORD):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def login_as_new(client, role=Role.CUSTOMER, username="customer1"):
    user = make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)
    login(client, username)
    return user


def add_to_cart(client, item_id: int, quantity=1):
    return client.post("/cart/add", data={"menu_item_id": item_id, "quantity": quantity}, follow_redirects=False)


# --- Cart (1-10) -------------------------------------------------------------


def test_1_empty_cart(client, db):
    resp = client.get("/cart")
    assert resp.status_code == 200
    assert "Your cart is empty." in resp.get_data(as_text=True)

    view = build_cart_view({})
    assert view.lines == []
    assert view.subtotal == Decimal("0.00")


def test_2_add_valid_item(client, db):
    item = make_menu_item(price="150.00")
    resp = add_to_cart(client, item.id, 2)
    assert resp.status_code == 302

    body = client.get("/cart").get_data(as_text=True)
    assert item.name in body
    assert "300.00" in body  # 150.00 x 2, server-computed


def test_3_add_unavailable_item_rejected(client, db):
    item = make_menu_item(is_available=False)
    add_to_cart(client, item.id, 1)
    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body

    with pytest.raises(CartError) as exc_info:
        add_item({}, item.id, 1)
    assert "menu_item_id" in exc_info.value.errors


def test_4_invalid_menu_id_rejected(client, db):
    add_to_cart(client, 999999, 1)
    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body

    with pytest.raises(CartError) as exc_info:
        add_item({}, 999999, 1)
    assert "menu_item_id" in exc_info.value.errors


def test_5_zero_quantity_rejected(client, db):
    item = make_menu_item()
    add_to_cart(client, item.id, 0)
    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body

    with pytest.raises(CartError):
        add_item({}, item.id, 0)


def test_6_negative_quantity_rejected(client, db):
    item = make_menu_item()
    add_to_cart(client, item.id, -3)
    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body

    with pytest.raises(CartError):
        add_item({}, item.id, -3)


def test_7_excessive_quantity_rejected(client, db):
    item = make_menu_item()
    add_to_cart(client, item.id, MAX_QUANTITY_PER_ITEM + 1)
    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body

    with pytest.raises(CartError):
        add_item({}, item.id, MAX_QUANTITY_PER_ITEM + 1)


def test_8_cart_update(client, db):
    item = make_menu_item(price="20.00")
    add_to_cart(client, item.id, 1)
    resp = client.post("/cart/update", data={"menu_item_id": item.id, "quantity": 5}, follow_redirects=False)
    assert resp.status_code == 302

    body = client.get("/cart").get_data(as_text=True)
    assert "100.00" in body  # 20.00 x 5

    cart = update_item({str(item.id): 1}, item.id, 5)
    assert cart[str(item.id)] == 5


def test_9_cart_removal(client, db):
    item = make_menu_item()
    add_to_cart(client, item.id, 1)
    resp = client.post("/cart/remove", data={"menu_item_id": item.id}, follow_redirects=False)
    assert resp.status_code == 302

    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body
    assert remove_item({str(item.id): 1}, item.id) == {}


def test_10_cart_clear(client, db):
    item1 = make_menu_item(name="Item One")
    item2 = make_menu_item(category=item1.category, name="Item Two")
    add_to_cart(client, item1.id, 1)
    add_to_cart(client, item2.id, 1)

    resp = client.post("/cart/clear", data={}, follow_redirects=False)
    assert resp.status_code == 302

    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body


# --- Checkout (11-19) ----------------------------------------------------------


def test_11_checkout_requires_authentication(client, db):
    item = make_menu_item()
    add_to_cart(client, item.id, 1)
    assert client.get("/checkout").status_code == 401
    assert client.post("/checkout", data={}).status_code == 401
    assert _db.session.query(Order).count() == 0


def test_12_successful_checkout(client, db):
    category = make_category()
    item = make_menu_item(category=category, price="99.00")
    login_as_new(client, username="checkout_customer")
    add_to_cart(client, item.id, 2)

    resp = client.post("/checkout", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/orders/1")

    order = _db.session.query(Order).one()
    assert order.status == OrderStatus.PENDING
    assert len(order.items) == 1


def test_13_correct_server_calculated_subtotal(client, db):
    category = make_category()
    item1 = make_menu_item(category=category, name="A", price="10.50")
    item2 = make_menu_item(category=category, name="B", price="4.25")
    user = login_as_new(client, username="subtotal_customer")
    add_to_cart(client, item1.id, 3)  # 31.50
    add_to_cart(client, item2.id, 2)  # 8.50

    order = checkout(user.id, {str(item1.id): 3, str(item2.id): 2})
    assert order.subtotal == Decimal("40.00")


def test_14_correct_server_calculated_total(client, db):
    category = make_category()
    item = make_menu_item(category=category, price="19.99")
    user = login_as_new(client, username="total_customer")

    order = checkout(user.id, {str(item.id): 3})
    assert order.total == Decimal("59.97")
    assert order.total == order.subtotal  # no tax/fee in this milestone


def test_15_client_price_manipulation_ignored(client, db):
    item = make_menu_item(price="199.00")
    user = login_as_new(client, username="price_hacker")

    # The checkout form has no price field at all -- there is no code path
    # for a client to submit one. Extra POST fields are simply ignored.
    resp = client.post(
        "/checkout",
        data={"unit_price": "0.01", "menu_item_id": item.id, "quantity": 1},
        follow_redirects=False,
    )
    assert resp.status_code == 302  # cart is empty -- nothing to check out
    assert _db.session.query(Order).count() == 0

    add_to_cart(client, item.id, 1)
    client.post("/checkout", data={"unit_price": "0.01"}, follow_redirects=False)
    order = _db.session.query(Order).one()
    assert order.items[0].unit_price_snapshot == Decimal("199.00")


def test_16_client_subtotal_manipulation_ignored(client, db):
    item = make_menu_item(price="50.00")
    login_as_new(client, username="subtotal_hacker")
    add_to_cart(client, item.id, 2)

    client.post("/checkout", data={"subtotal": "0.01"}, follow_redirects=False)
    order = _db.session.query(Order).one()
    assert order.subtotal == Decimal("100.00")


def test_17_client_total_manipulation_ignored(client, db):
    item = make_menu_item(price="50.00")
    login_as_new(client, username="total_hacker")
    add_to_cart(client, item.id, 2)

    client.post("/checkout", data={"total": "0.01"}, follow_redirects=False)
    order = _db.session.query(Order).one()
    assert order.total == Decimal("100.00")


def test_18_price_snapshot_stored(client, db):
    item = make_menu_item(name="Snapshot Special", price="75.00")
    user = login_as_new(client, username="snapshot_customer")

    order = checkout(user.id, {str(item.id): 1})
    order_item = order.items[0]
    assert order_item.item_name_snapshot == "Snapshot Special"
    assert order_item.unit_price_snapshot == Decimal("75.00")

    # Changing the live menu item afterward must never rewrite history.
    item.name = "Renamed"
    item.price = Decimal("999.00")
    _db.session.commit()

    _db.session.expire_all()
    reloaded = _db.session.get(OrderItem, order_item.id)
    assert reloaded.item_name_snapshot == "Snapshot Special"
    assert reloaded.unit_price_snapshot == Decimal("75.00")


def test_19_transaction_rollback_on_failure(db, monkeypatch):
    item = make_menu_item(price="30.00")
    user = make_user(username="rollback_customer", email="rollback@example.com", password=PASSWORD)

    def boom(*args, **kwargs):
        raise SQLAlchemyError("simulated commit failure")

    monkeypatch.setattr(_db.session, "commit", boom)

    with pytest.raises(CheckoutError):
        checkout(user.id, {str(item.id): 1})

    monkeypatch.undo()  # restore the real commit before querying
    assert _db.session.query(Order).count() == 0
    assert _db.session.query(OrderItem).count() == 0


# --- Order history / IDOR (20-22) ---------------------------------------------


def test_20_order_history_shows_only_current_users_orders(client, db):
    item = make_menu_item(price="10.00")
    user_a = make_user(username="alice_orders", email="alice_orders@example.com", password=PASSWORD)
    user_b = make_user(username="bob_orders", email="bob_orders@example.com", password=PASSWORD)
    order_a = checkout(user_a.id, {str(item.id): 1})
    order_b = checkout(user_b.id, {str(item.id): 1})

    login(client, "alice_orders")
    body = client.get("/orders").get_data(as_text=True)
    assert f"Order #{order_a.id}" in body
    assert f"Order #{order_b.id}" not in body

    assert list_orders_for_user(user_a.id) == [order_a]


def test_21_idor_attempt_rejected(client, db):
    item = make_menu_item(price="10.00")
    owner = make_user(username="owner", email="owner@example.com", password=PASSWORD)
    attacker = make_user(username="attacker", email="attacker@example.com", password=PASSWORD)
    order = checkout(owner.id, {str(item.id): 1})

    login(client, "attacker")
    resp = client.get(f"/orders/{order.id}")
    assert resp.status_code == 404  # not 403 -- existence is not disclosed either
    assert get_order_for_user(order.id, attacker.id) is None
    assert get_order_for_user(order.id, owner.id) is not None


def test_22_customer_cannot_change_order_status(client, db):
    item = make_menu_item(price="10.00")
    user = login_as_new(client, username="status_customer")
    add_to_cart(client, item.id, 1)
    client.post("/checkout", data={"status": "completed"}, follow_redirects=False)

    order = _db.session.query(Order).one()
    assert order.status == OrderStatus.PENDING

    # No route exists for a customer to change status.
    assert client.post(f"/orders/{order.id}/status", data={"status": "completed"}).status_code == 404
    assert client.post(f"/orders/{order.id}", data={"status": "completed"}).status_code in (404, 405)


# --- Security / audit (23-28) -------------------------------------------------


def test_23_csrf_required_for_state_changing_cart_and_checkout_routes(app, client, db):
    app.config["WTF_CSRF_ENABLED"] = True
    item = make_menu_item(price="10.00")

    resp = client.post("/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    assert resp.status_code == 400

    login_as_new(client, username="csrf_customer")
    resp = client.post("/checkout", data={})
    assert resp.status_code == 400


def test_24_order_creation_audit_event(client, db):
    item = make_menu_item(price="42.00")
    user = login_as_new(client, username="audit_customer")
    add_to_cart(client, item.id, 1)
    client.post("/checkout", data={}, follow_redirects=False)

    order = _db.session.query(Order).one()
    events = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_CREATED).all()
    assert len(events) == 1
    assert events[0].user_id == user.id
    assert str(order.id) in events[0].metadata_json
    assert PASSWORD not in (events[0].metadata_json or "")


def test_25_unavailable_item_rejected_during_checkout(client, db):
    item = make_menu_item(price="10.00")
    user = login_as_new(client, username="unavailable_customer")
    add_to_cart(client, item.id, 1)

    # Item is deactivated after being added to the cart (e.g. by staff).
    item.is_available = False
    _db.session.commit()

    resp = client.post("/checkout", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert _db.session.query(Order).count() == 0

    failures = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_CREATION_FAILED).all()
    assert len(failures) == 1
    assert failures[0].user_id == user.id

    with pytest.raises(CheckoutError):
        checkout(user.id, {str(item.id): 1})


def test_26_quantity_boundaries(client, db):
    item = make_menu_item(price="5.00")

    with pytest.raises(CartError):
        add_item({}, item.id, MIN_QUANTITY - 1)
    cart = add_item({}, item.id, MIN_QUANTITY)
    assert cart[str(item.id)] == MIN_QUANTITY

    cart = add_item(cart, item.id, MAX_QUANTITY_PER_ITEM - MIN_QUANTITY)  # top up to exactly the max
    assert cart[str(item.id)] == MAX_QUANTITY_PER_ITEM
    with pytest.raises(CartError):
        add_item(cart, item.id, 1)  # one more pushes over the max


def test_27_decimal_monetary_calculation_has_no_float_drift(client, db):
    item = make_menu_item(price="10.10")
    user = make_user(username="decimal_customer", email="decimal@example.com", password=PASSWORD)

    order = checkout(user.id, {str(item.id): 3})
    assert order.subtotal == Decimal("30.30")
    assert str(order.subtotal) == "30.30"  # not float drift like 30.299999999999997

    _db.session.expire_all()
    reloaded = _db.session.get(Order, order.id)
    assert str(reloaded.total) == "30.30"


def test_28_cart_cleared_after_successful_checkout(client, db):
    item = make_menu_item(price="10.00")
    login_as_new(client, username="clear_after_checkout")
    add_to_cart(client, item.id, 1)

    client.post("/checkout", data={}, follow_redirects=False)

    body = client.get("/cart").get_data(as_text=True)
    assert "Your cart is empty." in body
