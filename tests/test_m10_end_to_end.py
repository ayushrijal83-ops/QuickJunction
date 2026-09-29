"""Milestone 10 end-to-end: customer -> staff -> admin through real HTTP routes.

One scenario, in order, with a separate client per role. Runs on SQLite by
default and on MySQL when TEST_DATABASE_URL points there.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from flask import g

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import CancellationActor, Order, OrderSource, OrderStatus
from app.models.reservation import Reservation, ReservationStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role
from app.services.reservations import create_reservation
from app.services.sales import build_report
from app.utils.clock import db_today
from tests.conftest import make_menu_item, make_user

PASSWORD = "correct-horse-1"


def req(client, method, url, **kw):
    """Every request as its own identity (known issue #29: the test app
    context caches g.current_user across clients)."""
    g.pop("current_user", None)
    return getattr(client, method)(url, **kw)


def order_via_checkout(client, item, source, table=None) -> Order:
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 2})
    data = {"source": source}
    if table is not None:
        data["table_id"] = str(table.id)
    response = req(client, "post", "/checkout", data=data)
    assert response.status_code == 302, response.get_data(as_text=True)[:300]
    return _db.session.query(Order).order_by(Order.id.desc()).first()


def test_m10_customer_staff_admin_flow(app, db):
    item = make_menu_item(name="Chicken Burger", price="385.00")
    t01 = RestaurantTable(name="T01", capacity=4)
    t02 = RestaurantTable(name="T02", capacity=2)
    _db.session.add_all([t01, t02])
    _db.session.commit()
    alice = make_user(username="alice", email="alice@example.com", password=PASSWORD)
    bob = make_user(username="bob", email="bob@example.com", password=PASSWORD)
    make_user(username="cook", email="cook@example.com", password=PASSWORD, role=Role.STAFF)
    make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    tomorrow = (db_today() + timedelta(days=1)).isoformat()
    bobs = create_reservation(bob, t02.id, tomorrow, "13:00", 2)

    cust, staff, admin = app.test_client(), app.test_client(), app.test_client()

    # --- CUSTOMER (1-12) ---------------------------------------------------------
    r = req(cust, "post", "/login", data={"username": "alice", "password": PASSWORD})
    assert r.headers["Location"].endswith("/account/")                             # 1
    dash = req(cust, "get", "/account/").get_data(as_text=True)
    assert "Tables free right now" in dash and "T01" in dash                        # 2-4
    search = req(cust, "get", f"/reservations/new?reservation_date={tomorrow}&reservation_time=19:00&guest_count=3")
    assert "T01" in search.get_data(as_text=True)                                   # 4 (slot-aware)
    r = req(cust, "post", "/reservations", data={"table_id": t01.id, "reservation_date": tomorrow,
                                                  "reservation_time": "19:00", "guest_count": 3, "user_id": bob.id})
    confirmation = req(cust, "get", r.headers["Location"]).get_data(as_text=True)
    assert "Reservation confirmed" in confirmation and "alice" in confirmation      # 5
    mine = _db.session.query(Reservation).filter_by(user_id=alice.id).one()
    assert (mine.table_id, mine.guest_count, mine.reserved_at is not None) == (t01.id, 3, True)
    assert "T01" in req(cust, "get", "/reservations").get_data(as_text=True)        # 6
    assert req(cust, "get", f"/reservations/{bobs.id}").status_code == 404         # 7
    assert req(cust, "post", f"/reservations/{bobs.id}/cancel").status_code == 404

    dine_in = order_via_checkout(cust, item, "dine_in", t02)                         # 8
    assert (dine_in.source, dine_in.table.name, dine_in.user_id) == (OrderSource.DINE_IN, "T02", alice.id)  # 9
    assert req(cust, "post", f"/orders/{dine_in.id}/cancel", data={"reason": "Changed plans"}).status_code == 302  # 10
    _db.session.expire_all()
    dine_in = _db.session.get(Order, dine_in.id)
    assert dine_in.status == OrderStatus.CANCELLED                                   # 11
    assert (dine_in.cancellation_actor, dine_in.cancelled_by_id, dine_in.cancellation_reason) == (
        CancellationActor.CUSTOMER, alice.id, "Changed plans")                        # 12
    assert dine_in.cancelled_at is not None and dine_in.table_id == t02.id

    second = order_via_checkout(cust, item, "dine_in", t01)   # the one staff will prepare
    takeaway = order_via_checkout(cust, item, "takeaway")
    assert takeaway.table_id is None

    # --- STAFF (13-19) -------------------------------------------------------------
    req(staff, "post", "/login/staff", data={"username": "cook", "password": PASSWORD})
    board = req(staff, "get", "/staff/tables").get_data(as_text=True)               # 14
    assert f"Order #{second.id}" in board and f"Order #{dine_in.id}" not in board   # cancelled isn't active
    res_page = req(staff, "get", "/staff/reservations").get_data(as_text=True)      # 15
    assert "alice" in res_page and "bob" in res_page and "@example.com" not in res_page
    for status in ("confirmed", "preparing"):                                       # 16
        req(staff, "post", f"/staff/orders/{second.id}/status", data={"status": status})
    r = req(cust, "post", f"/orders/{second.id}/cancel")                            # 17
    assert r.status_code == 409
    _db.session.expire_all()
    assert _db.session.get(Order, second.id).status == OrderStatus.PREPARING
    for status in ("ready", "served", "completed"):
        req(staff, "post", f"/staff/orders/{second.id}/status", data={"status": status})
    for status in ("confirmed", "preparing", "ready", "completed"):
        req(staff, "post", f"/staff/orders/{takeaway.id}/status", data={"status": status})

    for step in ("cleaning", "available"):                                          # 18
        req(staff, "post", f"/staff/tables/{t01.id}/status", data={"status": step})
    req(staff, "post", f"/staff/tables/{t01.id}/status", data={"status": "cleaning"})  # 19: AVAILABLE -> CLEANING illegal
    _db.session.expire_all()
    assert _db.session.get(RestaurantTable, t01.id).status == TableStatus.AVAILABLE
    assert req(staff, "get", "/admin/reports").status_code == 403

    # --- ADMIN (20-27) -------------------------------------------------------------
    req(admin, "post", "/login/admin", data={"username": "boss", "password": PASSWORD})
    assert req(admin, "get", "/admin/reports?range=today").status_code == 200       # 20
    today = db_today()
    report = build_report(today, today, "Today")
    two_burgers = Decimal("770.00")
    assert report.totals.net == report.totals.gross == 2 * two_burgers              # 21
    assert (report.placed, report.totals.orders, report.cancelled, report.active) == (3, 2, 1, 0)  # 22
    assert report.totals.net != 3 * two_burgers                                     # 23 cancelled not a sale
    sources = {s: (t.orders, t.net) for s, t, _ in report.by_source}
    assert sources[OrderSource.DINE_IN] == sources[OrderSource.TAKEAWAY] == (1, two_burgers)  # 24
    tables = {tbl.name: (t.orders, t.net) for tbl, t in report.by_table}
    assert tables == {"T01": (1, two_burgers), "T02": (0, Decimal("0.00"))}         # 25
    assert [(d, p, t.net) for d, p, t in report.daily] == [(today, 3, 2 * two_burgers)]  # 26
    assert sum(t.orders for _, t in report.by_hour) == 2                            # 27
    page = req(admin, "get", "/admin/reports?range=today").get_data(as_text=True)
    assert "1540.00" in page

    # --- historical safety (section 11) -------------------------------------------
    item.price = Decimal("999.00")
    _db.session.commit()
    _db.session.expire_all()
    done = _db.session.get(Order, second.id)
    # total includes the 13 % tax snapshotted at checkout (M12): 770.00 + 100.10
    assert (done.total, done.items[0].unit_price_snapshot, done.table.name) == (Decimal("870.10"), Decimal("385.00"), "T01")
    assert _db.session.get(Order, dine_in.id).cancellation_reason == "Changed plans"
    mine = _db.session.get(Reservation, mine.id)
    r = req(cust, "post", f"/reservations/{mine.id}/cancel")
    _db.session.expire_all()
    mine = _db.session.get(Reservation, mine.id)
    assert (mine.status, mine.holds_slot, mine.guest_count) == (ReservationStatus.CANCELLED, None, 3)  # kept, slot freed
    create_reservation(bob, t01.id, tomorrow, "19:00", 2)  # the freed slot is bookable again

    events = {row.event_type for row in _db.session.query(AuditLog)}
    assert {AuditEvent.RESERVATION_CREATED, AuditEvent.ORDER_STATUS_CHANGED, AuditEvent.TABLE_STATUS_CHANGED,
            AuditEvent.RESERVATION_STATUS_CHANGED, AuditEvent.ORDER_STATUS_CHANGE_REJECTED} <= events
    customer_cancel = [json.loads(r.metadata_json) for r in _db.session.query(AuditLog)
                       .filter_by(event_type=AuditEvent.ORDER_STATUS_CHANGED, user_id=alice.id)]
    assert customer_cancel == [{"order_id": dine_in.id, "from": "pending", "to": "cancelled", "actor": "customer"}]
