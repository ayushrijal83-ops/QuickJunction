"""Milestone 14: the kitchen screen -- queue, actions through the existing
atomic transition, timestamps, timing stats, privacy and authorization."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from flask import g
from sqlalchemy.orm.attributes import set_committed_value

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order, OrderSource, OrderStatus
from app.models.user import Role
from app.services.kitchen import KITCHEN_COLUMNS, queue, stats
from app.services.orders import OrderStatusError, cancel_order_as_customer, update_order_status
from app.services.tables import create_table
from app.utils.clock import db_now
from tests.conftest import csrf_token, make_user
from tests.test_restaurant_ops import customer, place, set_status

PASSWORD = "correct-horse-1"


def member(name="cook", role=Role.STAFF):
    return make_user(username=name, email=f"{name}@example.com", password=PASSWORD, role=role)


def login(client, name):
    g.pop("current_user", None)  # known issue #29
    client.post("/login", data={"username": name, "password": PASSWORD})


def status_of(order_id):
    _db.session.expire_all()
    return _db.session.get(Order, order_id)


# --- queue ----------------------------------------------------------------------------


def test_queue_groups_active_orders_oldest_first_and_hides_finished_ones(db):
    user = customer()
    orders = {s: place(user) for s in (*KITCHEN_COLUMNS, OrderStatus.SERVED, OrderStatus.COMPLETED, OrderStatus.CANCELLED)}
    for s, o in orders.items():
        set_status(o, s)
    older = place(user)
    older.created_at = datetime(2020, 1, 1)
    _db.session.commit()

    columns = queue()
    assert list(columns) == list(KITCHEN_COLUMNS)
    assert columns[OrderStatus.PENDING] == [older, orders[OrderStatus.PENDING]]  # oldest first
    for s in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
        assert columns[s] == [orders[s]]
    shown = {o.id for col in columns.values() for o in col}
    for gone in (OrderStatus.SERVED, OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        assert orders[gone].id not in shown


def test_queue_is_one_query_per_relationship_not_per_order(app, db):
    user = customer()
    for _ in range(8):
        place(user)
    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    _db.session.expire_all()
    sa.event.listen(_db.engine, "before_cursor_execute", count)
    try:
        columns = queue()
        _ = [(o.items[0].item_name_snapshot, o.table) for col in columns.values() for o in col]
    finally:
        sa.event.remove(_db.engine, "before_cursor_execute", count)
    assert len(statements) <= 3, statements  # orders + items (+ tables): flat regardless of queue length


# --- timestamps -----------------------------------------------------------------------


def test_transitions_stamp_preparation_times(db):
    order = place(customer())
    update_order_status(order, OrderStatus.CONFIRMED)
    assert order.preparing_at is None
    update_order_status(order, OrderStatus.PREPARING)
    assert order.preparing_at is not None and order.ready_at is None
    update_order_status(order, OrderStatus.READY)
    assert order.ready_at is not None and order.ready_at >= order.preparing_at
    stamped = order.preparing_at
    update_order_status(order, OrderStatus.COMPLETED)
    assert order.preparing_at == stamped  # later steps never rewrite it


def test_a_losing_transition_stamps_nothing(db):
    """The customer cancelled; a kitchen tablet still showing CONFIRMED taps
    'Start preparing'. The stale transition must fail and leave no stamp."""
    user = customer()
    order = place(user)
    set_status(order, OrderStatus.CONFIRMED)
    cancel_order_as_customer(order, user)
    set_committed_value(order, "status", OrderStatus.CONFIRMED)
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.PREPARING)
    row = status_of(order.id)
    assert (row.status, row.preparing_at) == (OrderStatus.CANCELLED, None)


# --- stats ---------------------------------------------------------------------------------


def test_stats_average_prep_time_and_longest_waiting(db):
    user = customer()
    now = db_now()
    done = []
    for minutes_taken in (10, 20):
        o = place(user)
        o.status, o.preparing_at, o.ready_at = OrderStatus.COMPLETED, now - timedelta(minutes=minutes_taken + 5), now - timedelta(minutes=5)
        done.append(o)
    yesterday = place(user)
    yesterday.status = OrderStatus.COMPLETED
    yesterday.preparing_at, yesterday.ready_at = now - timedelta(days=2, minutes=90), now - timedelta(days=2)
    waiting_long, waiting_short = place(user), place(user)
    waiting_long.created_at = now - timedelta(minutes=45)
    _db.session.commit()

    s = stats(queue(), now.date())
    assert s.prepared_today == 2
    assert s.average_prep == timedelta(minutes=15)
    assert s.longest_active.id == waiting_long.id


def test_stats_with_nothing_prepared(db):
    s = stats(queue(), db_now().date())
    assert (s.prepared_today, s.average_prep, s.longest_active) == (0, None, None)


# --- routes -----------------------------------------------------------------------------------


def test_kitchen_rbac(app, client, db):
    assert client.get("/staff/kitchen").status_code == 401
    customer()
    login(client, "diner")
    assert client.get("/staff/kitchen").status_code == 403
    for name, role in (("cook", Role.STAFF), ("boss", Role.ADMIN)):
        c = app.test_client()
        member(name, role)
        login(c, name)
        assert c.get("/staff/kitchen").status_code == 200


def test_board_shows_what_to_cook_but_no_customer_data(client, db):
    user = customer("private_person")
    table = create_table("T07", 4)
    order = place(user, price="120.00", qty=3, source=OrderSource.DINE_IN, table_id=table.id)
    member()
    login(client, "cook")
    body = client.get("/staff/kitchen").get_data(as_text=True)
    assert f"#{order.id}" in body and "3 &times;" in body and order.items[0].item_name_snapshot in body
    assert "T07" in body and "Dine-in" in body
    assert "private_person" not in body and "@example.com" not in body
    assert '<meta http-equiv="refresh" content="30">' in body


def test_kitchen_buttons_drive_the_real_workflow_and_return_to_the_board(client, db):
    order = place(customer())
    cook = member()
    login(client, "cook")
    for expected in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
        r = client.post(f"/staff/orders/{order.id}/status", data={"status": expected.value, "return_to": "kitchen"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/staff/kitchen")
        assert status_of(order.id).status == expected
    rows = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_STATUS_CHANGED, user_id=cook.id).count()
    assert rows == 3


def test_return_to_is_an_allow_list_not_a_url(client, db):
    order = place(customer())
    member()
    login(client, "cook")
    for evil in ("https://evil.example/", "//evil.example", "/admin/settings", "KITCHEN"):
        r = client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed", "return_to": evil})
        assert r.headers["Location"].endswith(f"/staff/orders/{order.id}")
        set_status(order, OrderStatus.PENDING)


def test_kitchen_cannot_bypass_the_workflow(client, db):
    order = place(customer())
    member()
    login(client, "cook")
    client.post(f"/staff/orders/{order.id}/status", data={"status": "ready", "return_to": "kitchen"})  # skip
    assert status_of(order.id).status == OrderStatus.PENDING
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_STATUS_CHANGE_REJECTED).count() == 1


def test_customer_cannot_post_kitchen_actions(client, db):
    order = place(customer())
    login(client, "diner")
    r = client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed", "return_to": "kitchen"})
    assert r.status_code == 403 and status_of(order.id).status == OrderStatus.PENDING


def test_kitchen_actions_require_csrf(app, client, db):
    order = place(customer())
    member()
    login(client, "cook")
    app.config["WTF_CSRF_ENABLED"] = True
    client.post(f"/staff/orders/{order.id}/status", data={"status": "confirmed", "return_to": "kitchen"})
    assert status_of(order.id).status == OrderStatus.PENDING
    token = csrf_token(client, "/staff/kitchen")
    client.post(f"/staff/orders/{order.id}/status",
                data={"status": "confirmed", "return_to": "kitchen", "csrf_token": token})
    assert status_of(order.id).status == OrderStatus.CONFIRMED


def test_cancelled_order_leaves_the_board(client, db):
    user = customer()
    order = place(user)
    member()
    login(client, "cook")
    assert f"#{order.id}" in client.get("/staff/kitchen").get_data(as_text=True)
    cancel_order_as_customer(order, user)
    assert f"#{order.id}<" not in client.get("/staff/kitchen").get_data(as_text=True)
