"""M3 -- staff order entry (/staff/orders/new).

Authorization, the table-seating rule, server-side pricing, quantity bounds,
availability, audit atomicity, and the M2 customer boundary. The two-staff
same-table race on real MySQL lives in tests/test_mysql_concurrency.py.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from flask import g

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order, OrderSource, OrderStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role
from app.services.orders import CheckoutError, checkout, place_staff_order
from app.services.pricing import current_settings, price
from app.services.tables import change_table_status, create_table
from app.utils.ratelimit import login_limiter
from tests.conftest import make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"
NEW = "/staff/orders/new"


@pytest.fixture(autouse=True)
def _clean_login_limiter():
    login_limiter._attempts.clear()
    yield
    login_limiter._attempts.clear()


def req(client, method, url, **kw):
    """Each request as its own identity (known issue #29)."""
    g.pop("current_user", None)
    return getattr(client, method)(url, **kw)


def user(username, role=Role.CUSTOMER):
    return make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)


def signed_in(app, username, role):
    account = user(username, role)
    client = app.test_client()
    req(client, "post", "/login", data={"username": username, "password": PASSWORD})
    return client, account


def fresh(model, pk):
    _db.session.expire_all()
    return _db.session.get(model, pk)


def expected_total(subtotal: Decimal) -> Decimal:
    return price(subtotal, Decimal("0.00"), current_settings()[0])[1]


@pytest.fixture
def menu(db):
    category = make_category("Mains")
    return (make_menu_item(category=category, name="Burger", price="250.00"),
            make_menu_item(category=category, name="Cola", price="100.00"))


@pytest.fixture
def table(db):
    return create_table("T04", 4)


@pytest.fixture
def staff(app, db):
    return signed_in(app, "cook", Role.STAFF)


def order_form(items: dict, source="dine_in", table=None, action="create", **extra):
    data = {"source": source, "action": action, **extra}
    if table is not None:
        data["table_id"] = str(table.id if hasattr(table, "id") else table)
    data.update({f"qty_{item.id}": str(qty) for item, qty in items.items()})
    return data


def orders() -> list[Order]:
    _db.session.expire_all()
    return _db.session.query(Order).all()


# =============================================================================
# authorization
# =============================================================================


def test_anonymous_is_refused(client, menu, table):
    burger, _ = menu
    assert req(client, "get", NEW).status_code == 401
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 401
    browser = req(client, "get", NEW, headers={"Accept": "text/html"})
    assert browser.status_code == 302 and "/login" in browser.headers["Location"]
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


def test_customer_cannot_reach_staff_order_entry(app, menu, table):
    burger, _ = menu
    client, _ = signed_in(app, "alice", Role.CUSTOMER)
    assert req(client, "get", NEW).status_code == 403
    for form in (order_form({burger: 1}, table=table),
                 order_form({burger: 1}, source="takeaway"),
                 order_form({burger: 1}, table=table, action="review")):
        assert req(client, "post", NEW, data=form).status_code == 403
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


@pytest.mark.parametrize("role", [Role.STAFF, Role.ADMIN])
def test_staff_and_admin_can_open_order_entry(app, menu, role):
    client, _ = signed_in(app, "worker", role)
    page = req(client, "get", NEW)
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Burger" in body and 'value="dine_in"' in body and 'value="takeaway"' in body
    assert 'value="online"' not in body and 'value="delivery"' not in body


def test_service_refuses_a_customer_actor(menu, table):
    burger, _ = menu
    with pytest.raises(CheckoutError):
        place_staff_order(user("alice"), {str(burger.id): 1}, "dine_in", table.id)
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


# =============================================================================
# creating orders
# =============================================================================


def test_staff_dine_in_creates_order_seats_table_and_audits(staff, menu, table):
    client, cook = staff
    burger, cola = menu
    response = req(client, "post", NEW, data=order_form({burger: 2, cola: 2}, table=table))
    assert response.status_code == 302
    [order] = orders()
    assert response.headers["Location"].endswith(f"/staff/orders/{order.id}")
    assert (order.source, order.table_id, order.user_id, order.status) == (
        OrderSource.DINE_IN, table.id, cook.id, OrderStatus.PENDING)
    assert sorted((i.item_name_snapshot, i.quantity, i.line_total) for i in order.items) == [
        ("Burger", 2, Decimal("500.00")), ("Cola", 2, Decimal("200.00"))]
    assert order.subtotal == Decimal("700.00") and order.total == expected_total(Decimal("700.00"))
    assert fresh(RestaurantTable, table.id).status == TableStatus.OCCUPIED

    [row] = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_CREATED).all()
    assert row.user_id == cook.id
    assert json.loads(row.metadata_json) == {"order_id": order.id, "total": str(order.total), "source": "dine_in",
                                             "table_id": table.id, "actor": "staff"}


def test_staff_order_flows_into_existing_workflows(staff, menu, table):
    client, _ = staff
    burger, _ = menu
    req(client, "post", NEW, data=order_form({burger: 1}, table=table))
    [order] = orders()
    assert req(client, "get", f"/staff/orders/{order.id}").status_code == 200
    queue = req(client, "get", "/staff/orders").get_data(as_text=True)
    assert f"#{order.id}" in queue and "Walk-in" in queue
    assert f"#{order.id}" in req(client, "get", "/staff/kitchen").get_data(as_text=True)
    assert f"Order #{order.id}" in req(client, "get", "/staff/tables").get_data(as_text=True)
    for status in ("confirmed", "preparing", "ready", "served", "completed"):
        req(client, "post", f"/staff/orders/{order.id}/status", data={"status": status})
    assert fresh(Order, order.id).status == OrderStatus.COMPLETED
    assert fresh(RestaurantTable, table.id).status == TableStatus.CLEANING  # released as before


def test_admin_can_create_a_dine_in_order(app, menu, table):
    client, boss = signed_in(app, "boss", Role.ADMIN)
    burger, _ = menu
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 302
    [order] = orders()
    assert order.user_id == boss.id and order.source == OrderSource.DINE_IN


def test_staff_takeaway_needs_no_table(staff, menu, table):
    client, _ = staff
    burger, _ = menu
    assert req(client, "post", NEW, data=order_form({burger: 3}, source="takeaway")).status_code == 302
    [order] = orders()
    assert (order.source, order.table_id, order.subtotal) == (OrderSource.TAKEAWAY, None, Decimal("750.00"))
    assert fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


def test_takeaway_with_a_table_is_refused(staff, menu, table):
    client, _ = staff
    burger, _ = menu
    assert req(client, "post", NEW, data=order_form({burger: 1}, source="takeaway", table=table)).status_code == 409
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


@pytest.mark.parametrize("source", ["", "online", "delivery", "drive_thru"])
def test_only_dine_in_and_takeaway_are_staff_sources(staff, menu, source):
    client, _ = staff
    burger, _ = menu
    assert req(client, "post", NEW, data=order_form({burger: 1}, source=source)).status_code == 409
    assert orders() == []
    assert len(_db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_CREATION_FAILED).all()) == 1


def test_dine_in_without_a_table_is_refused(staff, menu):
    client, _ = staff
    burger, _ = menu
    assert req(client, "post", NEW, data=order_form({burger: 1})).status_code == 409
    assert orders() == []


def test_review_shows_server_totals_and_writes_nothing(staff, menu, table):
    client, _ = staff
    burger, cola = menu
    page = req(client, "post", NEW, data=order_form({burger: 2, cola: 2}, table=table, action="review",
                                                    total="1.00"))
    body = page.get_data(as_text=True)
    assert page.status_code == 200 and "Current order" in body and "700.00" in body and "Create order" in body
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


# =============================================================================
# tables
# =============================================================================


@pytest.mark.parametrize("table_id", ["999999", "abc", "-1", "1 OR 1=1", "0"])
def test_nonexistent_or_malformed_table_is_refused(staff, menu, table, table_id):
    client, _ = staff
    burger, _ = menu
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table_id)).status_code == 409
    assert orders() == []


@pytest.mark.parametrize("status", ["occupied", "out_of_service"])
def test_unseatable_table_status_is_refused(staff, menu, table, status):
    client, _ = staff
    burger, _ = menu
    change_table_status(table, status)
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 409
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus(status)


def test_cleaning_table_is_refused(staff, menu, table):
    client, _ = staff
    burger, _ = menu
    change_table_status(table, "occupied")
    change_table_status(table, "cleaning")
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 409
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.CLEANING


def test_reserved_table_can_be_seated_as_the_lifecycle_allows(staff, menu, table):
    """RESERVED -> OCCUPIED is an allowed transition: staff seat the party the
    table was held for."""
    client, _ = staff
    burger, _ = menu
    change_table_status(table, "reserved")
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 302
    assert fresh(RestaurantTable, table.id).status == TableStatus.OCCUPIED


def test_table_with_an_open_order_is_never_seated_twice(staff, menu, table):
    """Staff may set an OCCUPIED table back to AVAILABLE by hand while its
    order is still open; seating a second party there is still refused."""
    client, _ = staff
    burger, _ = menu
    req(client, "post", NEW, data=order_form({burger: 1}, table=table))
    change_table_status(fresh(RestaurantTable, table.id), "available")
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 409
    assert len(orders()) == 1


def test_stale_page_cannot_seat_a_table_taken_meanwhile(staff, menu, table):
    client, _ = staff
    burger, _ = menu
    assert f'value="{table.id}"' in req(client, "get", NEW).get_data(as_text=True)  # free when rendered
    checkout(user("other_cook", Role.STAFF).id, {str(burger.id): 1}, OrderSource.DINE_IN, table.id)
    response = req(client, "post", NEW, data=order_form({burger: 1}, table=table))
    assert response.status_code == 409 and "not free to seat" in response.get_data(as_text=True)
    assert len(orders()) == 1


def test_page_offers_only_tables_free_to_seat(staff, db):
    client, _ = staff
    free, held, busy, closed = (create_table(n, 4) for n in ("TA", "TB", "TC", "TD"))
    change_table_status(held, "reserved")
    change_table_status(busy, "occupied")
    change_table_status(closed, "out_of_service")
    body = req(client, "get", NEW).get_data(as_text=True)
    assert f'value="{free.id}"' in body and f'value="{held.id}"' in body
    assert f'value="{busy.id}"' not in body and f'value="{closed.id}"' not in body


# =============================================================================
# tampering, quantities, availability
# =============================================================================


def test_posted_prices_and_totals_are_ignored(staff, menu, table):
    client, _ = staff
    burger, _ = menu
    req(client, "post", NEW, data=order_form({burger: 2}, table=table, price="1", unit_price="1", subtotal="1",
                                             total="1", line_total="1", tax_amount="0", discount_amount="499"))
    [order] = orders()
    assert order.items[0].unit_price_snapshot == Decimal("250.00")
    assert (order.subtotal, order.discount_amount) == (Decimal("500.00"), Decimal("0.00"))
    assert order.total == expected_total(Decimal("500.00"))


@pytest.mark.parametrize("qty", ["0", "-1", "abc", "1.5", "1e3", "21", "999999999", "2 2", "0x10"])
def test_bad_quantities_are_refused(staff, menu, table, qty):
    client, _ = staff
    burger, _ = menu
    response = req(client, "post", NEW, data={**order_form({}, table=table), f"qty_{burger.id}": qty})
    assert response.status_code == 400
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


@pytest.mark.parametrize("qty", ["1", "20", " 7 "])
def test_quantity_bounds_are_inclusive(staff, menu, qty):
    client, _ = staff
    burger, _ = menu
    req(client, "post", NEW, data={**order_form({}, source="takeaway"), f"qty_{burger.id}": qty})
    [order] = orders()
    assert order.items[0].quantity == int(qty)


def test_empty_order_is_refused(staff, menu, table):
    client, _ = staff
    assert req(client, "post", NEW, data=order_form({}, table=table)).status_code == 400
    assert orders() == []


def test_unknown_unavailable_or_hidden_items_are_refused(staff, table, db):
    client, _ = staff
    off_menu = make_menu_item(category=make_category("Specials"), name="Sold out", is_available=False)
    hidden = make_menu_item(category=make_category("Retired", is_active=False), name="Old dish")
    for item_id in (off_menu.id, hidden.id, 999999):
        response = req(client, "post", NEW, data={**order_form({}, table=table), f"qty_{item_id}": "1"})
        assert response.status_code == 409
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE
    page = req(client, "get", NEW).get_data(as_text=True)
    assert "Sold out" not in page and "Old dish" not in page


def test_price_changed_after_review_uses_the_current_menu_price(staff, menu):
    client, _ = staff
    burger, _ = menu
    req(client, "post", NEW, data=order_form({burger: 1}, source="takeaway", action="review"))
    burger.price = Decimal("300.00")
    _db.session.commit()
    req(client, "post", NEW, data=order_form({burger: 1}, source="takeaway"))
    [order] = orders()
    assert order.subtotal == Decimal("300.00")


def test_item_withdrawn_after_review_is_refused(staff, menu, table):
    client, _ = staff
    burger, _ = menu
    req(client, "post", NEW, data=order_form({burger: 1}, table=table, action="review"))
    burger.is_available = False
    _db.session.commit()
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 409
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


# =============================================================================
# atomicity
# =============================================================================


def test_audit_failure_rolls_back_order_and_table(staff, menu, table, monkeypatch):
    client, _ = staff
    burger, _ = menu

    def boom(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr("app.routes.staff_orders.record_event", boom)
    assert req(client, "post", NEW, data=order_form({burger: 1}, table=table)).status_code == 500
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ORDER_CREATED).count() == 0


def test_failed_order_leaves_no_partial_state(staff, menu, table, monkeypatch):
    """A database failure after the table was locked and the order flushed."""
    client, _ = staff
    burger, _ = menu
    from sqlalchemy.exc import OperationalError

    from app.services.audit import record_event as real

    def broken(event, **kw):
        if event == AuditEvent.ORDER_CREATED:
            raise OperationalError("INSERT", {}, Exception("lost connection"))
        return real(event, **kw)

    monkeypatch.setattr("app.routes.staff_orders.record_event", broken)
    response = req(client, "post", NEW, data=order_form({burger: 1}, table=table))
    assert response.status_code == 409
    body = response.get_data(as_text=True)
    assert "lost connection" not in body and "INSERT" not in body
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


# =============================================================================
# M2 customer boundary still closed
# =============================================================================


def test_customer_checkout_boundary_unchanged(app, menu, table):
    burger, _ = menu
    client, alice = signed_in(app, "alice", Role.CUSTOMER)

    def attempt(**form):
        req(client, "post", "/cart/add", data={"menu_item_id": burger.id, "quantity": 1})
        return req(client, "post", "/checkout", data=form)

    attempt(source="dine_in", table_id=str(table.id))
    attempt(source="takeaway", table_id=str(table.id))
    attempt(source="online", table_id=str(table.id))
    assert orders() == [] and fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE
    req(client, "post", "/cart/clear")
    attempt(source="takeaway")
    req(client, "post", "/cart/clear")
    attempt(source="online")
    assert sorted(o.source.value for o in orders()) == ["online", "takeaway"]
    assert all(o.user_id == alice.id and o.table_id is None for o in orders())
