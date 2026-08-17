"""Milestone 05 tests: staff order management, the status-transition
allow-list, and the authorization boundary around both.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order, OrderItem, OrderStatus
from app.models.user import Role
from app.services.orders import (
    OrderStatusError,
    allowed_next_statuses,
    checkout,
    is_valid_transition,
    update_order_status,
)
from tests.conftest import make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"


def login(client, username: str, password: str = PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


def make_and_login(client, role: Role, username: str):
    user = make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)
    login(client, username)
    return user


def place_order(username: str = "buyer", price: str = "100.00", quantity: int = 2) -> Order:
    """A real order created through the checkout service, so its price
    snapshots are genuine rather than hand-written. Each call gets its own
    category, since `categories.name` is UNIQUE and a single test may place
    several orders."""
    category = make_category(name=f"Category for {username}")
    item = make_menu_item(category=category, name=f"Dish for {username}", price=price)
    customer = make_user(username=username, email=f"{username}@example.com", password=PASSWORD)
    return checkout(customer.id, {str(item.id): quantity})


def set_status(order: Order, status: OrderStatus) -> None:
    """Move an order directly, bypassing the workflow -- for arranging a
    starting state without asserting on the transition itself."""
    order.status = status
    _db.session.commit()


# --- Access control (1-4) -----------------------------------------------------


def test_1_staff_routes_require_authentication(client, db):
    order = place_order()
    assert client.get("/staff/orders").status_code == 401
    assert client.get(f"/staff/orders/{order.id}").status_code == 401
    assert client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed"}).status_code == 401

    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


def test_2_customer_denied_staff_routes(client, db):
    order = place_order()
    make_and_login(client, Role.CUSTOMER, "nosy_customer")

    assert client.get("/staff/orders").status_code == 403
    assert client.get(f"/staff/orders/{order.id}").status_code == 403
    assert client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed"}).status_code == 403

    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


def test_3_staff_allowed_staff_routes(client, db):
    order = place_order()
    make_and_login(client, Role.STAFF, "kitchen_staff")

    assert client.get("/staff/orders").status_code == 200
    assert client.get(f"/staff/orders/{order.id}").status_code == 200


def test_4_admin_allowed_staff_routes(client, db):
    """ADMIN reuses the staff interface rather than getting its own."""
    order = place_order()
    make_and_login(client, Role.ADMIN, "boss_admin")

    assert client.get("/staff/orders").status_code == 200
    assert client.get(f"/staff/orders/{order.id}").status_code == 200


# --- Queue and detail (5-6) ---------------------------------------------------


def test_5_staff_order_list_shows_operational_fields(client, db):
    order = place_order(username="lister", price="12.50", quantity=4)
    make_and_login(client, Role.STAFF, "queue_staff")

    body = client.get("/staff/orders").get_data(as_text=True)
    assert f"#{order.id}" in body
    assert "lister" in body            # customer identifier
    assert "pending" in body           # status
    assert "50.00" in body             # total, 12.50 x 4
    assert ">1<" in body               # item count (one distinct line)
    # The customer's email is not operational data and must not leak here.
    assert "lister@example.com" not in body


def test_6_staff_order_detail_shows_snapshots(client, db):
    order = place_order(username="detailed", price="33.00", quantity=3)
    make_and_login(client, Role.STAFF, "detail_staff")

    body = client.get(f"/staff/orders/{order.id}").get_data(as_text=True)
    assert "Dish for detailed" in body  # item name snapshot
    assert "33.00" in body              # unit price snapshot
    assert "99.00" in body              # line total, subtotal and total
    assert "pending" in body
    assert str(order.created_at.year) in body


# --- Customer visibility (7-8) ------------------------------------------------


def test_7_customer_sees_own_order_status(client, db):
    order = place_order(username="viewer")
    set_status(order, OrderStatus.PREPARING)

    login(client, "viewer")
    assert "preparing" in client.get("/orders").get_data(as_text=True)
    assert "preparing" in client.get(f"/orders/{order.id}").get_data(as_text=True)


def test_8_customer_cannot_change_status(client, db):
    order = place_order(username="changer")
    login(client, "changer")

    # The staff endpoint is the only status-change route and it is role-gated.
    assert client.post(f"/staff/orders/{order.id}/status", data={"status": "completed"}).status_code == 403
    # No customer-facing status route exists at all.
    assert client.post(f"/orders/{order.id}/status", data={"status": "completed"}).status_code == 404
    assert client.post(f"/orders/{order.id}", data={"status": "completed"}).status_code in (404, 405)

    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


# --- Status workflow (9-10) ---------------------------------------------------


def test_9_valid_status_transition(client, db):
    order = place_order()
    make_and_login(client, Role.STAFF, "workflow_staff")

    for target in ("confirmed", "preparing", "ready", "completed"):
        resp = client.post(f"/staff/orders/{order.id}/status", data={"status": target})
        assert resp.status_code == 302
        _db.session.refresh(order)
        assert order.status == OrderStatus(target)


def test_10_invalid_status_transitions_rejected(client, db):
    make_and_login(client, Role.STAFF, "invalid_staff")

    # Backwards from a terminal state.
    completed = place_order(username="done_customer")
    set_status(completed, OrderStatus.COMPLETED)
    resp = client.post(f"/staff/orders/{completed.id}/status", data={"status": "preparing"})
    assert resp.status_code == 302  # redirected with a flashed error, not applied
    _db.session.refresh(completed)
    assert completed.status == OrderStatus.COMPLETED

    # Skipping the queue.
    pending = place_order(username="skip_customer")
    client.post(f"/staff/orders/{pending.id}/status", data={"status": "completed"})
    _db.session.refresh(pending)
    assert pending.status == OrderStatus.PENDING

    # A status outside the enum entirely.
    client.post(f"/staff/orders/{pending.id}/status", data={"status": "teleported"})
    _db.session.refresh(pending)
    assert pending.status == OrderStatus.PENDING

    # Cancelled is terminal too.
    cancelled = place_order(username="cancelled_customer")
    set_status(cancelled, OrderStatus.CANCELLED)
    client.post(f"/staff/orders/{cancelled.id}/status", data={"status": "confirmed"})
    _db.session.refresh(cancelled)
    assert cancelled.status == OrderStatus.CANCELLED


def test_10b_transition_table_directly(client, db):
    assert is_valid_transition(OrderStatus.PENDING, OrderStatus.CONFIRMED)
    assert is_valid_transition(OrderStatus.READY, OrderStatus.COMPLETED)
    assert is_valid_transition(OrderStatus.PENDING, OrderStatus.CANCELLED)

    assert not is_valid_transition(OrderStatus.COMPLETED, OrderStatus.PREPARING)
    assert not is_valid_transition(OrderStatus.PENDING, OrderStatus.COMPLETED)
    assert not is_valid_transition(OrderStatus.CANCELLED, OrderStatus.CONFIRMED)
    # A no-op "change" is not a valid transition either.
    assert not is_valid_transition(OrderStatus.PENDING, OrderStatus.PENDING)

    assert allowed_next_statuses(OrderStatus.COMPLETED) == []
    assert OrderStatus.CONFIRMED in allowed_next_statuses(OrderStatus.PENDING)

    order = place_order()
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.COMPLETED)
    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


# --- Security (11-13, 16) -----------------------------------------------------


def test_11_csrf_protects_status_changes(app, client, db):
    order = place_order()
    make_and_login(client, Role.STAFF, "csrf_staff")
    app.config["WTF_CSRF_ENABLED"] = True

    resp = client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed"})
    assert resp.status_code == 400
    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


def test_12_forged_role_headers_and_fields_ignored(client, db):
    order = place_order()
    make_and_login(client, Role.CUSTOMER, "forger")

    forged_headers = {"X-Role": "staff", "X-User-Role": "admin", "Role": "admin"}
    assert client.get("/staff/orders", headers=forged_headers).status_code == 403
    resp = client.post(
        f"/staff/orders/{order.id}/status",
        data={"status": "confirmed", "role": "admin", "is_staff": "true", "user_role": "admin"},
        headers=forged_headers,
    )
    assert resp.status_code == 403

    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


def test_13_customer_idor_protection_still_intact(client, db):
    victim_order = place_order(username="victim")
    make_and_login(client, Role.CUSTOMER, "attacker2")

    # Another customer's order is still invisible on the customer routes...
    assert client.get(f"/orders/{victim_order.id}").status_code == 404
    assert f"#{victim_order.id}" not in client.get("/orders").get_data(as_text=True)
    # ...and the staff route is not a way around that.
    assert client.get(f"/staff/orders/{victim_order.id}").status_code == 403


def test_16_unauthorized_status_update_rejected_without_side_effects(client, db):
    order = place_order()

    # Anonymous.
    assert client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed"}).status_code == 401
    # Authenticated but wrong role.
    make_and_login(client, Role.CUSTOMER, "unauthorized_user")
    assert client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed"}).status_code == 403

    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING
    # A denied attempt must not manufacture a "status changed" audit row.
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_STATUS_CHANGED).count() == 0


# --- Audit (14) ---------------------------------------------------------------


def test_14_audit_event_on_status_change(client, db):
    order = place_order()
    staff = make_and_login(client, Role.STAFF, "audited_staff")

    client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed"})

    events = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_STATUS_CHANGED).all()
    assert len(events) == 1
    entry = events[0]
    assert entry.user_id == staff.id  # the actor, not the customer
    assert entry.success is True
    assert str(order.id) in entry.metadata_json
    assert "pending" in entry.metadata_json and "confirmed" in entry.metadata_json
    assert entry.created_at is not None
    # No credential or session material anywhere in the row.
    blob = f"{entry.metadata_json} {entry.user_agent}"
    assert PASSWORD not in blob
    assert "password" not in blob.lower()


def test_14b_rejected_transition_is_audited_as_a_failure(client, db):
    order = place_order()
    set_status(order, OrderStatus.COMPLETED)
    staff = make_and_login(client, Role.STAFF, "reject_staff")

    client.post(f"/staff/orders/{order.id}/status", data={"status": "preparing"})

    rejected = _db.session.query(AuditLog).filter_by(
        event_type=AuditEvent.ORDER_STATUS_CHANGE_REJECTED
    ).all()
    assert len(rejected) == 1
    assert rejected[0].success is False
    assert rejected[0].user_id == staff.id
    assert str(order.id) in rejected[0].metadata_json
    # And nothing actually moved.
    _db.session.refresh(order)
    assert order.status == OrderStatus.COMPLETED


# --- Price integrity (15) -----------------------------------------------------


def test_15_status_changes_never_touch_price_snapshots(client, db):
    order = place_order(username="pricey", price="77.00", quantity=2)
    order_item = order.items[0]
    original = (
        order_item.item_name_snapshot,
        order_item.unit_price_snapshot,
        order_item.line_total,
        order.subtotal,
        order.total,
    )
    assert original[1] == Decimal("77.00")
    assert original[4] == Decimal("154.00")

    # The live menu item changes underneath the order...
    menu_item = order_item.menu_item
    menu_item.name = "Renamed After Ordering"
    menu_item.price = Decimal("999.00")
    _db.session.commit()

    # ...and the order is walked through the whole workflow.
    make_and_login(client, Role.STAFF, "price_staff")
    for target in ("confirmed", "preparing", "ready", "completed"):
        client.post(f"/staff/orders/{order.id}/status", data={"status": target})

    _db.session.expire_all()
    reloaded_order = _db.session.get(Order, order.id)
    reloaded_item = _db.session.get(OrderItem, order_item.id)
    assert reloaded_item.item_name_snapshot == original[0]
    assert reloaded_item.unit_price_snapshot == original[1]
    assert reloaded_item.line_total == original[2]
    assert reloaded_order.subtotal == original[3]
    assert reloaded_order.total == original[4]
    assert reloaded_order.status == OrderStatus.COMPLETED


# --- Information disclosure (12 in the security list) --------------------------


def test_17_staff_route_errors_do_not_expose_internals(app, client, db, monkeypatch):
    import app.routes.staff_orders as staff_routes

    def _boom():
        raise RuntimeError("should never reach the client")

    monkeypatch.setattr(staff_routes, "list_orders_for_staff", _boom)
    make_and_login(client, Role.STAFF, "error_staff")

    resp = client.get("/staff/orders")
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "RuntimeError" not in body
    assert "Traceback" not in body
    assert "should never reach the client" not in body
    assert resp.get_json()["error"]["message"] == "An internal error occurred."


def test_18_missing_order_is_404_not_an_error_page(client, db):
    make_and_login(client, Role.STAFF, "notfound_staff")
    assert client.get("/staff/orders/999999").status_code == 404
    assert client.post("/staff/orders/999999/status", data={"status": "confirmed"}).status_code == 404
