"""Milestone 15: navigation per role, dashboards and their data scope, the
read-only admin payments/audit pages, status badges and accessibility hooks.
Presentation tests -- but the RBAC ones assert the server's behaviour too."""

from __future__ import annotations

from datetime import timedelta

import pytest
from flask import g, render_template_string

from app.extensions import db as _db
from app.models.audit_log import AuditEvent
from app.models.order import OrderStatus
from app.models.user import Role, User
from app.services.audit import record_event
from app.services.inventory import record_movement, save_ingredient
from app.services.payments import record_payment, refund_payment
from app.services.reservations import create_reservation
from app.services.tables import create_table
from app.utils.clock import db_today
from app.utils.formatting import CURRENCY_SYMBOL
from tests.conftest import make_user
from tests.test_restaurant_ops import customer, place, set_status

PASSWORD = "correct-horse-1"

STAFF_LINKS = ("/staff/orders", "/staff/kitchen", "/staff/tables", "/staff/reservations", "/staff/inventory")
ADMIN_LINKS = ("/admin/reports", "/admin/payments", "/admin/audit", "/admin/settings", "/admin/staff", "/admin/menu")


def login(client, name, role=Role.CUSTOMER):
    make_user(username=name, email=f"{name}@example.com", password=PASSWORD, role=role)
    g.pop("current_user", None)  # known issue #29
    client.post("/login", data={"username": name, "password": PASSWORD})


def page(client, path="/account/"):
    g.pop("current_user", None)
    return client.get(path).get_data(as_text=True)


# --- navigation -----------------------------------------------------------------------


def test_anonymous_nav(client, db):
    body = page(client, "/menu")
    assert 'href="/menu"' in body and "Sign in" in body and "Register" in body and 'href="/cart' in body
    assert "Dashboard" not in body
    for link in STAFF_LINKS + ADMIN_LINKS:
        assert link not in body


def test_customer_nav_shows_only_customer_links(client, db):
    login(client, "cust")
    body = page(client)
    for link in ("/account/", "/menu", "/recommendations", "/orders", "/reservations", "/cart"):
        assert f'href="{link}' in body, link
    for link in STAFF_LINKS + ADMIN_LINKS:
        assert link not in body, link


def test_staff_nav_shows_operations_but_no_admin_or_customer_links(client, db):
    login(client, "cook", Role.STAFF)
    body = page(client)
    for link in STAFF_LINKS:
        assert f'href="{link}' in body, link
    for link in ADMIN_LINKS + ("/recommendations", "/preferences", 'href="/cart'):
        assert link not in body, link
    # ...and the server refuses them regardless -- except the menu list, which
    # STAFF may *view* by design since M03 (only ADMIN may change it).
    for link in ADMIN_LINKS:
        assert client.get(link).status_code == (200 if link == "/admin/menu" else 403), link


def test_admin_nav_has_everything_operational_and_management(client, db):
    login(client, "boss", Role.ADMIN)
    body = page(client)
    for link in STAFF_LINKS + ADMIN_LINKS:
        assert f'href="{link}' in body, link
    assert "/recommendations" not in body


def test_current_page_is_marked_for_assistive_tech(client, db):
    login(client, "cook", Role.STAFF)
    body = page(client, "/staff/kitchen")
    assert 'href="/staff/kitchen" aria-current="page"' in body
    assert 'href="/staff/tables" aria-current' not in body


def test_skip_link_and_main_landmark(client, db):
    body = page(client, "/menu")
    assert 'href="#main"' in body and 'id="main"' in body and 'aria-label="Main"' in body


def test_error_pages_still_render_the_layout(client, db):
    r = client.get("/definitely-not-here", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "navbar" in r.get_data(as_text=True)


# --- dashboards ---------------------------------------------------------------------------


def test_customer_dashboard_is_own_data_only(client, db):
    login(client, "cust")
    me = _db.session.query(User).filter_by(username="cust").one()
    mine = place(me)
    theirs = place(customer("someone_else"))
    table = create_table("T01", 4)
    create_reservation(me, table.id, (db_today() + timedelta(days=1)).isoformat(), "19:00", 2)
    body = page(client)
    assert "Your current order" in body and f"Order #{mine.id}" in body
    assert f"Order #{theirs.id}" not in body and "someone_else" not in body
    assert "Upcoming reservations" in body and "T01 &middot;" in body
    assert "Picked for you" in body and "/menu/" in body
    assert "Book a table" in body and "Tables free right now" in body


def test_staff_dashboard_is_operational_and_shows_no_money(client, db):
    boss = make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    order = place(customer())
    record_payment(order, "cash", boss)
    set_status(order, OrderStatus.PREPARING)
    salt = save_ingredient(None, "salt", "g", "100", True)
    record_movement(salt, "purchase", "10", "x", boss)
    login(client, "cook", Role.STAFF)
    body = page(client)
    assert "Kitchen now" in body and "Preparing" in body and "Order queue" in body
    assert "Low stock" in body and "salt" in body
    assert "Today&#39;s reservations" in body or "Today's reservations" in body
    assert CURRENCY_SYMBOL not in body  # sales are ADMIN-only; the dashboard does not widen that


def test_admin_dashboard_shows_the_business_view(client, db):
    login(client, "boss", Role.ADMIN)
    boss = _db.session.query(User).filter_by(username="boss").one()
    order = place(customer(), price="100.00", qty=1)
    record_payment(order, "card", boss)
    set_status(order, OrderStatus.COMPLETED)
    body = page(client)
    assert "Today&#39;s net sales" in body or "Today's net sales" in body
    assert f"{CURRENCY_SYMBOL}100.00" in body          # net sales (tax excluded)
    assert f"{CURRENCY_SYMBOL}113.00" in body          # collected (13 % tax included)
    assert "Kitchen now" in body and "0 pending" in body


# --- admin payments & audit -------------------------------------------------------------------


def test_payments_page_admin_only_and_lists_refunds(app, client, db):
    boss = make_user(username="boss2", email="b2@example.com", password=PASSWORD, role=Role.ADMIN)
    order = place(customer(), price="100.00", qty=1)
    p = record_payment(order, "wallet", boss)
    refund_payment(p, "13.00", "cold soup")
    assert client.get("/admin/payments").status_code == 401
    login(client, "cust")
    assert client.get("/admin/payments").status_code == 403
    staff = app.test_client()
    login(staff, "cook", Role.STAFF)
    assert staff.get("/admin/payments").status_code == 403
    admin = app.test_client()
    login(admin, "boss", Role.ADMIN)
    body = page(admin, "/admin/payments")
    assert f"#{order.id}" in body and "Wallet" in body and "113.00" in body and "13.00" in body
    assert "&ldquo;cold soup&rdquo;" in body and "boss2" in body


def test_audit_page_admin_only_filters_by_allow_list_and_escapes(app, client, db):
    record_event(AuditEvent.LOGIN_FAILURE, success=False, metadata={"username": "<script>alert(1)</script>"})
    login(client, "cook", Role.STAFF)
    assert client.get("/admin/audit").status_code == 403
    admin = app.test_client()
    login(admin, "boss", Role.ADMIN)
    body = page(admin, "/admin/audit")
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body
    filtered = page(admin, "/admin/audit?event=login_failure")
    assert "login failure" in filtered and "login success" not in filtered.split("<tbody>")[1]
    hostile = admin.get("/admin/audit?event=' OR 1=1 --")
    assert hostile.status_code == 200  # unknown value falls back to "all", never reaches SQL


# --- status badges -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(OrderStatus))
def test_every_order_status_has_its_own_badge(app, db, status):
    def render(s):
        with app.test_request_context():  # context processors (cart count) need a request
            return render_template_string('{% include "partials/_status_badge.html" %}', status=s)

    html = render(status)
    assert status.value in html and "badge" in html
    others = {s: render(s) for s in OrderStatus}
    classes = {s: h.split('class="')[1].split('"')[0] for s, h in others.items()}
    assert list(classes.values()).count(classes[status]) == 1  # colour pair unique to this status


@pytest.mark.parametrize("role, links", [
    (Role.CUSTOMER, ("/account/", "/menu", "/recommendations", "/orders", "/reservations", "/reservations/new", "/cart")),
    (Role.STAFF, ("/account/",) + STAFF_LINKS),
    (Role.ADMIN, ("/account/",) + STAFF_LINKS + ADMIN_LINKS),
])
def test_every_nav_destination_renders_for_its_role(client, db, role, links):
    """No link a role is shown may lead to an error or a 403."""
    login(client, f"crawler_{role.value}", role)
    for link in links:
        g.pop("current_user", None)
        assert client.get(link).status_code == 200, link