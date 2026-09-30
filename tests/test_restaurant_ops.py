"""Milestone 10 (brief M08): customer cancellation, the lifecycle and its
concurrency guard, restaurant tables, and order source/table rules.
"""

from __future__ import annotations

import itertools
import json

import pytest
import sqlalchemy as sa
from flask import g
from sqlalchemy.orm.attributes import set_committed_value

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import CancellationActor, Order, OrderSource, OrderStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role
from app.services.orders import (
    CheckoutError,
    OrderStatusError,
    cancel_order_as_customer,
    checkout,
    update_order_status,
)
from app.services.tables import TableError, change_table_status, create_table
from tests.conftest import csrf_token, rejected_by_check_constraint, make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"


def login(client, username: str):
    g.pop("current_user", None)  # known issue #29: identity cache survives in tests
    return client.post("/login", data={"username": username, "password": PASSWORD})


def customer(username="diner"):
    return make_user(username=username, email=f"{username}@example.com", password=PASSWORD)


_seq = itertools.count()


def place(user, price="100.00", qty=2, source=OrderSource.ONLINE, table_id=None) -> Order:
    category = make_category(name=f"Cat {next(_seq)}")
    item = make_menu_item(category=category, name=f"Dish {category.name}", price=price)
    return checkout(user.id, {str(item.id): qty}, source, table_id)


def set_status(order: Order, status: OrderStatus) -> None:
    order.status = status
    _db.session.commit()


def force_db_status(order: Order, status: OrderStatus) -> None:
    """Another worker's committed write to the row."""
    _db.session.execute(sa.text("UPDATE orders SET status = :s WHERE id = :i"), {"s": status.value, "i": order.id})
    _db.session.commit()


def stale_view(order: Order, status: OrderStatus) -> None:
    """Make this request's in-memory ``order`` believe ``status`` -- what it
    read before the other worker committed -- without writing anything."""
    set_committed_value(order, "status", status)


def audit_rows(event: AuditEvent):
    return [json.loads(r.metadata_json) for r in _db.session.query(AuditLog).filter_by(event_type=event)]


# --- customer cancellation ---------------------------------------------------


@pytest.mark.parametrize("status", [OrderStatus.PENDING, OrderStatus.CONFIRMED])
def test_customer_can_cancel_own_order_before_preparation(client, db, status):
    user = customer()
    order = place(user)
    set_status(order, status)
    login(client, "diner")

    response = client.post(f"/orders/{order.id}/cancel", data={"reason": "Ordered by mistake"})
    assert response.status_code == 302
    _db.session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.cancellation_actor == CancellationActor.CUSTOMER
    assert order.cancelled_by_id == user.id
    assert order.cancelled_at is not None
    assert order.cancellation_reason == "Ordered by mistake"
    assert audit_rows(AuditEvent.ORDER_STATUS_CHANGED)[-1] == {
        "order_id": order.id, "from": status.value, "to": "cancelled", "actor": "customer"}


@pytest.mark.parametrize("status", [OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.COMPLETED])
def test_customer_cannot_cancel_once_preparation_started(client, db, status):
    order = place(customer())
    set_status(order, status)
    login(client, "diner")

    response = client.post(f"/orders/{order.id}/cancel")
    assert response.status_code == 409
    _db.session.refresh(order)
    assert order.status == status
    assert order.cancelled_at is None
    assert audit_rows(AuditEvent.ORDER_STATUS_CHANGE_REJECTED)[-1]["actor"] == "customer"


def test_customer_cannot_cancel_twice(client, db):
    user = customer()
    order = place(user)
    cancel_order_as_customer(order, user)
    first_cancelled_at = order.cancelled_at
    login(client, "diner")

    response = client.post(f"/orders/{order.id}/cancel")
    assert response.status_code == 409
    assert "already cancelled" in response.get_json()["error"]["message"]
    _db.session.refresh(order)
    assert order.cancelled_at == first_cancelled_at


def test_customer_cannot_cancel_another_customers_order(client, db):
    victim_order = place(customer("victim"))
    customer("attacker")
    login(client, "attacker")

    assert client.post(f"/orders/{victim_order.id}/cancel").status_code == 404
    _db.session.refresh(victim_order)
    assert victim_order.status == OrderStatus.PENDING


def test_service_rejects_non_owner_even_if_route_is_bypassed(db):
    victim_order = place(customer("victim"))
    with pytest.raises(OrderStatusError):
        cancel_order_as_customer(victim_order, customer("attacker"))
    _db.session.refresh(victim_order)
    assert victim_order.status == OrderStatus.PENDING


def test_nonexistent_order_is_404(client, db):
    customer()
    login(client, "diner")
    assert client.post("/orders/999999/cancel").status_code == 404


def test_unauthenticated_cancel_is_401(client, db):
    order = place(customer())
    assert client.post(f"/orders/{order.id}/cancel").status_code == 401
    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


def test_cancel_requires_csrf(app, client, db):
    order = place(customer())
    login(client, "diner")
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post(f"/orders/{order.id}/cancel").status_code == 400
    token = csrf_token(client, f"/orders/{order.id}")
    assert client.post(f"/orders/{order.id}/cancel", data={"csrf_token": token}).status_code == 302
    _db.session.refresh(order)
    assert order.status == OrderStatus.CANCELLED


def test_overlong_reason_rejected(client, db):
    order = place(customer())
    login(client, "diner")
    client.post(f"/orders/{order.id}/cancel", data={"reason": "x" * 256})
    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


def test_cancel_button_only_shown_while_cancellable(client, db):
    order = place(customer())
    login(client, "diner")
    assert "Cancel order" in client.get(f"/orders/{order.id}").get_data(as_text=True)
    set_status(order, OrderStatus.PREPARING)
    assert "Cancel order" not in client.get(f"/orders/{order.id}").get_data(as_text=True)


def test_customer_cannot_use_staff_status_route(client, db):
    order = place(customer())
    login(client, "diner")
    assert client.post(f"/staff/orders/{order.id}/status", data={"status": "cancelled"}).status_code == 403
    _db.session.refresh(order)
    assert order.status == OrderStatus.PENDING


# --- staff cancellation preserved, now recorded -----------------------------


@pytest.mark.parametrize("role, actor", [(Role.STAFF, CancellationActor.STAFF), (Role.ADMIN, CancellationActor.ADMIN)])
def test_staff_and_admin_cancellation_still_works_and_is_recorded(client, db, role, actor):
    order = place(customer())
    set_status(order, OrderStatus.PREPARING)  # staff may still cancel here; customers may not
    member = make_user(username="member", email="m@example.com", password=PASSWORD, role=role)
    login(client, "member")

    client.post(f"/staff/orders/{order.id}/status", data={"status": "cancelled", "reason": "Out of stock"})
    _db.session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert (order.cancellation_actor, order.cancelled_by_id, order.cancellation_reason) == (actor, member.id, "Out of stock")
    assert audit_rows(AuditEvent.ORDER_STATUS_CHANGED)[-1]["actor"] == role.value


# --- lifecycle ------------------------------------------------------------------


def test_full_valid_lifecycle(db):
    order = place(customer())
    for status in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.COMPLETED):
        update_order_status(order, status)
        assert order.status == status


@pytest.mark.parametrize("start, target", [
    (OrderStatus.COMPLETED, OrderStatus.PREPARING),
    (OrderStatus.CANCELLED, OrderStatus.CONFIRMED),
    (OrderStatus.READY, OrderStatus.PENDING),
    (OrderStatus.READY, OrderStatus.CANCELLED),
    (OrderStatus.PENDING, OrderStatus.READY),
])
def test_invalid_transitions_rejected(db, start, target):
    order = place(customer())
    set_status(order, start)
    with pytest.raises(OrderStatusError):
        update_order_status(order, target)
    _db.session.refresh(order)
    assert order.status == start


# --- race: customer cancel vs staff start-preparing ---------------------------


def test_staff_wins_race_customer_cancel_is_rejected(db):
    """The customer's request read CONFIRMED; the kitchen committed PREPARING
    before the customer's cancel executed."""
    user = customer()
    order = place(user)
    set_status(order, OrderStatus.CONFIRMED)
    force_db_status(order, OrderStatus.PREPARING)
    stale_view(order, OrderStatus.CONFIRMED)

    with pytest.raises(OrderStatusError):
        cancel_order_as_customer(order, user)
    _db.session.expire_all()
    row = _db.session.get(Order, order.id)
    assert row.status == OrderStatus.PREPARING
    assert row.cancelled_at is None and row.cancellation_actor is None


def test_customer_wins_race_staff_prepare_is_rejected(db):
    user = customer()
    order = place(user)
    set_status(order, OrderStatus.CONFIRMED)
    cancel_order_as_customer(order, user)
    stale_view(order, OrderStatus.CONFIRMED)  # the kitchen's request read it before the cancel

    with pytest.raises(OrderStatusError) as excinfo:
        update_order_status(order, OrderStatus.PREPARING)
    assert "changed by someone else" in excinfo.value.errors["status"][0]
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).status == OrderStatus.CANCELLED


def test_two_staff_cannot_double_transition(db):
    """Closes known issue #10: the second of two concurrent moves from the
    same read no longer silently wins."""
    order = place(customer())
    set_status(order, OrderStatus.CONFIRMED)
    force_db_status(order, OrderStatus.PREPARING)  # the other cook got there first
    stale_view(order, OrderStatus.CONFIRMED)
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.CANCELLED)
    assert order.status == OrderStatus.PREPARING  # refreshed to the truth


# --- tables ---------------------------------------------------------------------


def test_table_creation_and_unique_name(db):
    table = create_table(" T01 ", 4)
    assert (table.name, table.capacity, table.status) == ("T01", 4, TableStatus.AVAILABLE)
    with pytest.raises(TableError):
        create_table("T01", 2)


@pytest.mark.parametrize("capacity", [0, -1, 51, "four", None])
def test_capacity_validated(db, capacity):
    with pytest.raises(TableError):
        create_table("T09", capacity)


def test_database_enforces_capacity_and_status(db):
    with rejected_by_check_constraint():
        _db.session.execute(sa.text(
            "INSERT INTO restaurant_tables (name, capacity, status, created_at, updated_at)"
            " VALUES ('X', 0, 'available', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    _db.session.rollback()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text(
            "INSERT INTO restaurant_tables (name, capacity, status, created_at, updated_at)"
            " VALUES ('Y', 2, 'haunted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    _db.session.rollback()


def test_valid_and_invalid_table_status_changes(db):
    table = create_table("T02", 2)
    for step in ("reserved", "occupied", "cleaning", "available", "out_of_service", "available"):
        change_table_status(table, step)
    for bad in ("cleaning", "bogus"):  # AVAILABLE -> CLEANING is not allowed; bogus is unknown
        with pytest.raises(TableError):
            change_table_status(table, bad)
    assert table.status == TableStatus.AVAILABLE


# --- order source / table association ----------------------------------------


def test_dine_in_requires_table_and_occupies_it(db):
    user = customer()
    table = create_table("T05", 4)
    with pytest.raises(CheckoutError):
        place(user, source=OrderSource.DINE_IN)
    order = place(user, source=OrderSource.DINE_IN, table_id=table.id)
    assert (order.source, order.table.name) == (OrderSource.DINE_IN, "T05")
    assert table.status == TableStatus.OCCUPIED


@pytest.mark.parametrize("source", [OrderSource.TAKEAWAY, OrderSource.DELIVERY, OrderSource.ONLINE])
def test_other_sources_need_no_table_and_reject_one(db, source):
    user = customer()
    assert place(user, source=source).table_id is None
    table = create_table("T06", 2)
    with pytest.raises(CheckoutError):
        place(user, source=source, table_id=table.id)


def test_invalid_source_and_unseatable_table_rejected(db):
    user = customer()
    with pytest.raises(CheckoutError):
        place(user, source="drive_through")
    table = create_table("T07", 2)
    change_table_status(table, "out_of_service")
    with pytest.raises(CheckoutError):
        place(user, source=OrderSource.DINE_IN, table_id=table.id)
    with pytest.raises(CheckoutError):
        place(user, source=OrderSource.DINE_IN, table_id=424242)


def test_database_enforces_source_table_rule(db):
    order = place(customer())
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("UPDATE orders SET source = 'dine_in' WHERE id = :i"), {"i": order.id})
        _db.session.flush()
    _db.session.rollback()


def test_checkout_route_dine_in(client, db):
    table = create_table("T08", 4)
    item = make_menu_item()
    customer()
    login(client, "diner")
    client.post("/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    client.post("/checkout", data={"source": "dine_in", "table_id": str(table.id)})
    order = _db.session.query(Order).one()
    assert (order.source, order.table_id) == (OrderSource.DINE_IN, table.id)


def test_completed_order_keeps_its_table_after_table_is_freed(db):
    table = create_table("T03", 4)
    order = place(customer(), source=OrderSource.DINE_IN, table_id=table.id)
    for status in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.COMPLETED):
        update_order_status(order, status)
    assert table.status == TableStatus.CLEANING  # M11: released automatically on completion
    change_table_status(table, "available")
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).table.name == "T03"


# --- table routes: RBAC, board, audit ----------------------------------------


def test_table_rbac(app, client, db):
    table = create_table("T10", 2)
    assert client.get("/staff/tables").status_code == 401
    customer()
    login(client, "diner")
    assert client.get("/staff/tables").status_code == 403
    assert client.post(f"/staff/tables/{table.id}/status", data={"status": "reserved"}).status_code == 403
    assert client.get("/admin/tables/new").status_code == 403

    make_user(username="cook", email="cook@example.com", password=PASSWORD, role=Role.STAFF)
    client = app.test_client()
    login(client, "cook")
    assert client.get("/staff/tables").status_code == 200
    assert client.post("/admin/tables/new", data={"name": "T11", "capacity": 2}).status_code == 403
    assert client.post(f"/staff/tables/{table.id}/status", data={"status": "reserved"}).status_code == 302
    _db.session.refresh(table)
    assert table.status == TableStatus.RESERVED
    assert audit_rows(AuditEvent.TABLE_STATUS_CHANGED)[-1] == {"table_id": table.id, "from": "available", "to": "reserved"}


def test_admin_creates_table_and_board_shows_active_order(client, db):
    make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    login(client, "boss")
    client.post("/admin/tables/new", data={"name": "T12", "capacity": 6})
    table = _db.session.query(RestaurantTable).filter_by(name="T12").one()
    assert audit_rows(AuditEvent.TABLE_CREATED)[-1] == {"table_id": table.id}

    order = place(customer(), price="385.00", qty=3, source=OrderSource.DINE_IN, table_id=table.id)
    body = client.get("/staff/tables").get_data(as_text=True)
    assert f"Order #{order.id}" in body
    assert "1,305.15" in body  # 1155.00 + 13 % tax (M12)
    assert "occupied" in body
