"""Milestone 16: the complete demo, customer -> staff -> admin, through the
real HTTP routes only, spanning every milestone. Ends with the same data-
integrity checks that were run against the live database. Runs on SQLite and
on MySQL (TEST_DATABASE_URL)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa
from flask import g

from app.extensions import db as _db
from app.models import (
    AuditEvent, AuditLog, Ingredient, MenuItem, Order, OrderSource, OrderStatus, Payment,
    Reservation, ReservationStatus, RestaurantTable, Role, StockMovement, TableStatus, User,
)
from app.services.sales import build_report
from app.utils.clock import db_today
from tests.conftest import make_category, make_menu_item, make_user

PW = "correct-horse-1"
D = Decimal

INTEGRITY_CHECKS = {
    "order total formula": "SELECT COUNT(*) FROM orders WHERE ABS(total - (subtotal - discount_amount + tax_amount)) >= 0.005",
    "subtotal = sum of lines": "SELECT COUNT(*) FROM orders o WHERE o.subtotal <> "
                               "(SELECT COALESCE(SUM(line_total),0) FROM order_items i WHERE i.order_id=o.id)",
    "payment = order total": "SELECT COUNT(*) FROM payments p JOIN orders o ON o.id=p.order_id WHERE p.amount <> o.total",
    "no cancelled order holds money": "SELECT COUNT(*) FROM payments p JOIN orders o ON o.id=p.order_id "
                                      "WHERE o.status='cancelled' AND p.refunded_amount < p.amount",
    "stock = ledger": "SELECT COUNT(*) FROM ingredients i WHERE i.current_quantity <> "
                      "(SELECT COALESCE(SUM(quantity_change),0) FROM stock_movements m WHERE m.ingredient_id=i.id)",
    "sales only on completed orders": "SELECT COUNT(*) FROM stock_movements m JOIN orders o ON o.id=m.order_id "
                                      "WHERE o.status<>'completed'",
    "slot flag matches status": "SELECT COUNT(*) FROM reservations WHERE (status='cancelled' AND holds_slot IS NOT NULL) "
                                "OR (status<>'cancelled' AND holds_slot IS NULL)",
}


def go(client, _verb, _url, **data):
    g.pop("current_user", None)  # one app context per test (known issue #29)
    return getattr(client, _verb)(_url, data=data) if _verb == "post" else client.get(_url)


def text(client, url):
    return go(client, "get", url).get_data(as_text=True)


def fresh(model, id_):
    _db.session.expire_all()
    return _db.session.get(model, id_)


def test_full_demo_flow(app, db):
    # Setup that exists before the demo: an admin and a menu item.
    make_user(username="boss", email="boss@example.com", password=PW, role=Role.ADMIN)
    burger = make_menu_item(category=make_category(name="Mains"), name="Chicken Burger", price="350.00")
    admin, staff, cust, rival = (app.test_client() for _ in range(4))

    # --- ADMIN: settings, tables, stock, recipe ----------------------------------
    go(admin, "post", "/login/admin", username="boss", password=PW)
    assert "Admin dashboard" in text(admin, "/account/")
    go(admin, "post", "/admin/settings", tax_rate="13", staff_max_discount="15")
    for name, seats in (("T01", 4), ("T02", 2)):
        go(admin, "post", "/admin/tables/new", name=name, capacity=seats)
    go(admin, "post", "/admin/inventory/new", name="bun", unit="piece", minimum_quantity="5", is_active="y")
    bun = _db.session.query(Ingredient).filter_by(name="bun").one()
    go(admin, "post", f"/staff/inventory/{bun.id}/movement", movement_type="purchase", quantity="20", note="opening")
    go(admin, "post", f"/admin/menu/{burger.id}/recipe", add_ingredient_id=str(bun.id), add_quantity="1")
    t01, t02 = (_db.session.query(RestaurantTable).filter_by(name=n).one() for n in ("T01", "T02"))

    # --- STAFF: sign up, approved by admin ------------------------------------------
    go(staff, "post", "/register/staff", username="cook", email="cook@example.com", password=PW, password_confirm=PW)
    cook = _db.session.query(User).filter_by(username="cook").one()
    assert go(staff, "post", "/login/staff", username="cook", password=PW).status_code == 200  # pending: refused
    go(admin, "post", f"/admin/staff/{cook.id}/approve")
    assert go(staff, "post", "/login/staff", username="cook", password=PW).status_code == 302

    # --- CUSTOMER: register, browse, order, cancel, reserve --------------------------
    go(cust, "post", "/register", username="alice", email="alice@example.com", password=PW, password_confirm=PW)
    alice = _db.session.query(User).filter_by(username="alice").one()
    g.pop("current_user", None)
    if go(cust, "get", "/account/").status_code != 200:
        go(cust, "post", "/login", username="alice", password=PW)
    assert "Welcome, alice" in text(cust, "/account/")
    assert "Chicken Burger" in text(cust, "/menu")
    assert "Tax (13.00%)" in (go(cust, "post", "/cart/add", menu_item_id=burger.id, quantity=2) and text(cust, "/checkout"))
    go(cust, "post", "/checkout", source="dine_in", table_id=str(t01.id))
    first = _db.session.query(Order).filter_by(user_id=alice.id).order_by(Order.id.desc()).first()
    assert (first.source, first.table_id, first.subtotal, first.tax_amount, first.total) == (
        OrderSource.DINE_IN, t01.id, D("700.00"), D("91.00"), D("791.00"))
    assert fresh(RestaurantTable, t01.id).status == TableStatus.OCCUPIED
    go(cust, "post", f"/orders/{first.id}/cancel", reason="wrong table")
    first = fresh(Order, first.id)
    assert first.status == OrderStatus.CANCELLED and first.cancellation_actor.value == "customer"
    assert fresh(RestaurantTable, t01.id).status == TableStatus.CLEANING  # auto-released

    go(cust, "post", "/cart/add", menu_item_id=burger.id, quantity=3)
    go(cust, "post", "/checkout", source="takeaway")
    order = _db.session.query(Order).filter_by(user_id=alice.id).order_by(Order.id.desc()).first()
    assert (order.source, order.table_id, order.total) == (OrderSource.TAKEAWAY, None, D("1186.50"))

    tomorrow = (db_today() + timedelta(days=1)).isoformat()
    go(cust, "post", "/reservations", table_id=t02.id, reservation_date=tomorrow, reservation_time="19:00", guest_count=2)
    booking = _db.session.query(Reservation).filter_by(user_id=alice.id).one()
    assert "Reservation confirmed" in text(cust, f"/reservations/{booking.id}")
    go(rival, "post", "/register", username="bob", email="bob@example.com", password=PW, password_confirm=PW)
    g.pop("current_user", None)
    if go(rival, "get", "/account/").status_code != 200:
        go(rival, "post", "/login", username="bob", password=PW)
    assert go(rival, "get", f"/reservations/{booking.id}").status_code == 404        # not bob's
    assert go(rival, "post", f"/orders/{order.id}/cancel").status_code == 404          # not bob's

    # --- STAFF: kitchen, discount, payment ---------------------------------------------
    assert "Kitchen now" in text(staff, "/account/")
    assert f"#{order.id}" in text(staff, "/staff/kitchen") and "alice" not in text(staff, "/staff/kitchen")
    go(staff, "post", f"/staff/orders/{order.id}/discount", discount_type="percent", discount_value="20", discount_reason="x")
    assert fresh(Order, order.id).discount_amount == D("0.00")  # over the 15 % staff cap set by admin
    go(staff, "post", f"/staff/orders/{order.id}/discount", discount_type="percent", discount_value="10",
       discount_reason="regular customer")
    order = fresh(Order, order.id)
    assert (order.discount_amount, order.tax_amount, order.total) == (D("105.00"), D("122.85"), D("1067.85"))
    for step in ("confirmed", "preparing", "ready"):
        go(staff, "post", f"/staff/orders/{order.id}/status", status=step, return_to="kitchen")
    assert fresh(Order, order.id).ready_at is not None
    go(cust, "post", f"/orders/{order.id}/cancel")
    assert fresh(Order, order.id).status == OrderStatus.READY  # too late for the customer
    go(staff, "post", f"/staff/orders/{order.id}/payment", method="card", amount="1.00")
    go(staff, "post", f"/staff/orders/{order.id}/status", status="completed")
    order = fresh(Order, order.id)
    assert order.status == OrderStatus.COMPLETED and order.payment.amount == D("1067.85")
    assert fresh(Ingredient, bun.id).current_quantity == D("17")  # 20 - 3 burgers
    assert "Everything is stocked." in text(staff, "/account/")  # 17 buns, low at 5
    assert "stock" in text(staff, "/staff/inventory").lower()
    assert "19:00" in text(staff, "/staff/reservations")
    assert go(staff, "post", f"/staff/orders/{order.id}/refund", amount="10").status_code == 403

    # --- ADMIN: refund, reports, payments, audit ----------------------------------------
    go(admin, "post", f"/staff/orders/{order.id}/refund", amount="67.85", reason="late order")
    assert fresh(Payment, order.payment.id).refunded_amount == D("67.85")
    report = build_report(db_today(), db_today(), "Today")
    t = report.totals
    assert (t.orders, t.gross, t.discounts, t.tax, t.refunds) == (1, D("1050.00"), D("105.00"), D("122.85"), D("67.85"))
    assert t.net == D("877.15") and t.collected == D("1000.00") and report.cancelled == 1
    page = text(admin, "/admin/reports?range=today")
    assert "877.15" in page and "122.85" in page
    assert "late order" in text(admin, "/admin/payments")
    audit_page = text(admin, "/admin/audit")
    for event in ("payment refunded", "order discount applied", "pricing settings changed", "staff approved"):
        assert event in audit_page, event
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.RESERVATION_CREATED).count() == 1

    # --- historical integrity -------------------------------------------------------------
    burger_row = fresh(MenuItem, burger.id)
    burger_row.price = D("999.00")
    go(admin, "post", "/admin/settings", tax_rate="5", staff_max_discount="15")
    _db.session.commit()
    order = fresh(Order, order.id)
    assert (order.items[0].unit_price_snapshot, order.tax_rate, order.total) == (D("350.00"), D("13.00"), D("1067.85"))
    assert fresh(Reservation, booking.id).status == ReservationStatus.CONFIRMED
    assert _db.session.query(StockMovement).filter_by(order_id=order.id).count() == 1

    for name, sql in INTEGRITY_CHECKS.items():
        assert _db.session.execute(sa.text(sql)).scalar() == 0, name
