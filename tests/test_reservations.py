"""Milestone 10 (brief M08): table reservations and the customer dashboard."""

from __future__ import annotations

import json
import threading
from datetime import time, timedelta

import pytest
import sqlalchemy as sa
from flask import g

from app import create_app
from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import OrderStatus
from app.models.reservation import Reservation, ReservationStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role
from app.services.reservations import (
    ReservationError,
    available_tables,
    cancel_reservation_as_customer,
    create_reservation,
    update_reservation_status,
)
from app.services.tables import change_table_status, create_table
from app.utils.clock import db_today
from tests.conftest import csrf_token, make_user

PASSWORD = "correct-horse-1"
SEVEN_PM = "19:00"


def tomorrow() -> str:
    return (db_today() + timedelta(days=1)).isoformat()


def user(name="alice", role=Role.CUSTOMER):
    return make_user(username=name, email=f"{name}@example.com", password=PASSWORD, role=role)


def login(client, name):
    g.pop("current_user", None)  # known issue #29: identity cache survives in tests
    client.post("/login", data={"username": name, "password": PASSWORD})


def book(owner, table, day=None, at=SEVEN_PM, guests=2) -> Reservation:
    return create_reservation(owner, table.id, day or tomorrow(), at, guests)


# --- booking rules ------------------------------------------------------------------


def test_booking_records_owner_slot_and_server_timestamp(db):
    alice, table = user(), create_table("T04", 4)
    r = book(alice, table, guests=3)
    assert (r.user_id, r.table_id, r.guest_count, r.status) == (alice.id, table.id, 3, ReservationStatus.CONFIRMED)
    assert r.reservation_time == time(19) and r.reservation_date.isoformat() == tomorrow()
    assert r.reserved_at is not None and r.reserved_at.date() <= r.reservation_date
    # Booking a future slot never changes the table's current floor status.
    assert table.status == TableStatus.AVAILABLE


def test_same_table_same_slot_rejected_different_table_or_slot_ok(db):
    alice, bob = user(), user("bob")
    t1, t2 = create_table("T01", 4), create_table("T02", 4)
    book(alice, t1)
    with pytest.raises(ReservationError):
        book(bob, t1)
    assert book(bob, t2).table_id == t2.id          # different table, same time
    assert book(bob, t1, at="21:00").table_id == t1.id  # same table, next slot


def test_conflicting_non_slot_time_rejected(db):
    """19:30 would overlap the 19:00 booking; only slot starts are accepted,
    so an overlapping time cannot be booked at all."""
    alice, t1 = user(), create_table("T01", 4)
    book(alice, t1)
    with pytest.raises(ReservationError):
        book(user("bob"), t1, at="19:30")


def test_database_rejects_double_booking_even_without_the_service(db):
    alice, t1 = user(), create_table("T01", 4)
    r = book(alice, t1)
    with pytest.raises(sa.exc.IntegrityError):
        _db.session.add(Reservation(user_id=alice.id, table_id=t1.id, reservation_date=r.reservation_date,
                                    reservation_time=r.reservation_time, guest_count=1, holds_slot=True))
        _db.session.flush()
    _db.session.rollback()


def test_cancelled_booking_frees_the_slot(db):
    alice, t1 = user(), create_table("T01", 4)
    r = book(alice, t1)
    cancel_reservation_as_customer(r, alice)
    assert r.status == ReservationStatus.CANCELLED and r.holds_slot is None
    assert book(user("bob"), t1).status == ReservationStatus.CONFIRMED


@pytest.mark.parametrize("guests", [5, 0, -1, "many"])
def test_guest_count_and_capacity(db, guests):
    with pytest.raises(ReservationError):
        book(user(), create_table("T01", 4), guests=guests)


def test_out_of_service_table_rejected_but_occupied_now_is_bookable_later(db):
    alice = user()
    broken, busy = create_table("T01", 4), create_table("T02", 4)
    change_table_status(broken, "out_of_service")
    change_table_status(busy, "occupied")
    with pytest.raises(ReservationError):
        book(alice, broken)
    assert book(alice, busy).table_id == busy.id
    assert busy.status == TableStatus.OCCUPIED


@pytest.mark.parametrize("day, at", [
    ("yesterday", SEVEN_PM), ("2026-02-30", SEVEN_PM), ("not-a-date", SEVEN_PM),
    ("tomorrow", "18:00"), ("tomorrow", "25:00"), ("far", SEVEN_PM),
])
def test_date_and_time_validation(db, day, at):
    today = db_today()
    day = {"yesterday": (today - timedelta(days=1)).isoformat(), "tomorrow": tomorrow(),
           "far": (today + timedelta(days=90)).isoformat()}.get(day, day)
    with pytest.raises(ReservationError):
        book(user(), create_table("T01", 4), day=day, at=at)


def test_availability_is_slot_aware_not_status_aware(db):
    alice = user()
    small, big, booked = create_table("A2", 2), create_table("B6", 6), create_table("C4", 4)
    change_table_status(big, "occupied")
    change_table_status(big, "cleaning")  # current state only
    book(alice, booked)
    day = db_today() + timedelta(days=1)
    names = [t.name for t in available_tables(day, time(19), 3)]
    assert names == ["B6"]  # A2 too small, C4 booked; B6 is cleaning *now* but free at 19:00 tomorrow
    assert "C4" in [t.name for t in available_tables(day, time(21), 3)]


def test_staff_status_transitions(db):
    r = book(user(), create_table("T01", 4))
    update_reservation_status(r, "no_show")
    assert r.status == ReservationStatus.NO_SHOW and r.holds_slot is True
    for bad in ("confirmed", "cancelled", "bogus"):
        with pytest.raises(ReservationError):
            update_reservation_status(r, bad)


def test_concurrent_bookings_only_one_wins(tmp_path, monkeypatch):
    """Two real threads, two connections, one on-disk database: both submit
    the same table/slot at the same instant. Exactly one commits.

    The URI is patched *before* create_app: Flask-SQLAlchemy picks StaticPool
    (one shared connection) when the configured URI is in-memory, which would
    make the two threads share a connection and prove nothing."""
    from config import TestingConfig

    monkeypatch.setattr(TestingConfig, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    app = create_app("testing")
    with app.app_context():
        _db.create_all()
        table_id = create_table("T01", 4).id
        owners = [user("racer1").id, user("racer2").id]
        day = tomorrow()

    barrier, results = threading.Barrier(2), []

    def attempt(owner_id):
        with app.app_context():
            from app.models.user import User
            owner = _db.session.get(User, owner_id)
            barrier.wait()
            try:
                create_reservation(owner, table_id, day, SEVEN_PM, 2)
                results.append("ok")
            except ReservationError:
                results.append("conflict")
            except Exception as exc:  # surface anything else in the assertion
                results.append(repr(exc))
            finally:
                _db.session.remove()

    threads = [threading.Thread(target=attempt, args=(o,)) for o in owners]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["conflict", "ok"]
    with app.app_context():
        assert type(_db.engine.pool).__name__ != "StaticPool"
        assert _db.session.query(Reservation).count() == 1
        _db.session.remove()
        _db.engine.dispose()


# --- HTTP: ownership, spoofing, RBAC ----------------------------------------------


def test_owner_comes_from_session_not_form(client, db):
    alice, victim = user(), user("victim")
    table = create_table("T01", 4)
    login(client, "alice")
    response = client.post("/reservations", data={
        "table_id": table.id, "reservation_date": tomorrow(), "reservation_time": SEVEN_PM, "guest_count": 2,
        "user_id": victim.id, "customer_id": victim.id,
    }, headers={"X-User-Id": str(victim.id)})
    assert response.status_code == 302
    r = _db.session.query(Reservation).one()
    assert r.user_id == alice.id
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Reservation confirmed" in page and "alice" in page and "Reserved at" in page
    audit = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.RESERVATION_CREATED).one()
    assert audit.user_id == alice.id and json.loads(audit.metadata_json)["actor"] == "customer"


def test_customer_cannot_see_or_cancel_another_customers_reservation(client, db):
    victims = book(user("victim"), create_table("T01", 4))
    user()
    login(client, "alice")
    assert client.get(f"/reservations/{victims.id}").status_code == 404
    assert client.post(f"/reservations/{victims.id}/cancel").status_code == 404
    assert "T01" not in client.get("/reservations").get_data(as_text=True)
    _db.session.refresh(victims)
    assert victims.status == ReservationStatus.CONFIRMED


def test_customer_cancels_own_reservation_with_csrf(app, client, db):
    r = book(user(), create_table("T01", 4))
    login(client, "alice")
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post(f"/reservations/{r.id}/cancel").status_code == 400
    token = csrf_token(client, f"/reservations/{r.id}")
    assert client.post(f"/reservations/{r.id}/cancel", data={"csrf_token": token}).status_code == 302
    _db.session.refresh(r)
    assert r.status == ReservationStatus.CANCELLED


def test_conflict_over_http_is_409_for_api_clients(client, db):
    table = create_table("T01", 4)
    book(user("first"), table)
    user()
    login(client, "alice")
    response = client.post("/reservations", data={
        "table_id": table.id, "reservation_date": tomorrow(), "reservation_time": SEVEN_PM, "guest_count": 2})
    assert response.status_code == 409


def test_reservation_rbac(app, client, db):
    assert client.get("/reservations/new").status_code == 401
    assert client.get("/staff/reservations").status_code == 401
    user()
    login(client, "alice")
    assert client.get("/staff/reservations").status_code == 403
    staff_client = app.test_client()
    user("cook", Role.STAFF)
    login(staff_client, "cook")
    assert staff_client.get("/reservations/new").status_code == 403  # customer-only feature
    assert staff_client.post("/reservations", data={}).status_code == 403


def test_staff_list_shows_username_not_email_and_can_mark_completed(app, client, db):
    r = book(user(), create_table("T09", 4))
    user("cook", Role.STAFF)
    login(client, "cook")
    body = client.get("/staff/reservations").get_data(as_text=True)
    assert "alice" in body and "alice@example.com" not in body and "T09" in body
    client.post(f"/staff/reservations/{r.id}/status", data={"status": "completed"})
    _db.session.refresh(r)
    assert r.status == ReservationStatus.COMPLETED
    meta = json.loads(_db.session.query(AuditLog).filter_by(
        event_type=AuditEvent.RESERVATION_STATUS_CHANGED).one().metadata_json)
    assert meta == {"reservation_id": r.id, "from": "confirmed", "to": "completed", "actor": "staff"}


def test_search_page_lists_free_tables(client, db):
    create_table("T01", 2)
    create_table("T02", 6)
    user()
    login(client, "alice")
    body = client.get(f"/reservations/new?reservation_date={tomorrow()}&reservation_time=19:00&guest_count=4"
                      ).get_data(as_text=True)
    assert "T02" in body and "T01" not in body
    assert 'name="user_id"' not in body and 'name="customer_id"' not in body


# --- customer dashboard -------------------------------------------------------------


def test_customer_login_lands_on_dashboard_with_own_data_only(client, db):
    from tests.test_restaurant_ops import place

    alice, other = user(), user("other")
    mine, theirs = place(alice), place(other)
    table = create_table("T07", 4)
    book(alice, table)
    book(other, create_table("T08", 4))

    g.pop("current_user", None)
    response = client.post("/login", data={"username": "alice", "password": PASSWORD})
    assert response.headers["Location"].endswith("/account/")
    body = client.get("/account/").get_data(as_text=True)
    assert f"Order #{mine.id}" in body and f"Order #{theirs.id}" not in body
    assert "T07" in body and "T08 &middot;" not in body.split("Upcoming reservations")[1].split("Tables free")[0]
    assert "Book a table" in body and "Tables free right now" in body


def test_staff_and_admin_dashboards_unchanged(app, db):
    for name, role in (("cook", Role.STAFF), ("boss", Role.ADMIN)):
        c = app.test_client()
        user(name, role)
        login(c, name)
        body = c.get("/account/").get_data(as_text=True)
        assert "Order queue" in body and "Upcoming reservations" not in body


# --- SERVED lifecycle step ---------------------------------------------------------


def test_served_step_and_customer_cannot_cancel_served(client, db):
    from app.services.orders import OrderStatusError, cancel_order_as_customer, update_order_status
    from tests.test_restaurant_ops import place

    alice = user()
    order = place(alice)
    for status in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.SERVED):
        update_order_status(order, status)
    with pytest.raises(OrderStatusError):
        cancel_order_as_customer(order, alice)
    with pytest.raises(OrderStatusError):
        update_order_status(order, OrderStatus.CANCELLED)  # staff cannot cancel after serving either
    update_order_status(order, OrderStatus.COMPLETED)
    assert order.status == OrderStatus.COMPLETED
