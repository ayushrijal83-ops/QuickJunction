"""M2 -- Phase 1A security hardening regression tests.

One section per fix: proxy trust, customer checkout boundaries, the
reservation cap, audit atomicity, removed probe endpoints, Argon2 rehash,
failed-login audit sanitization and the LLM default. The reservation cap's
real-concurrency proof lives in tests/test_mysql_concurrency.py (MySQL only).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import time, timedelta

import pytest
from argon2 import PasswordHasher
from flask import g, jsonify, request, url_for

from app import create_app
from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order, OrderSource, OrderStatus
from app.models.reservation import Reservation, ReservationStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role, User
from app.services import local_llm
from app.services.audit import login_identifier, record_event
from app.services.auth import authenticate_user
from app.services.orders import CheckoutError, checkout, checkout_as_customer, update_order_status
from app.services.reservations import (
    ReservationError,
    cancel_reservation_as_customer,
    create_reservation,
)
from app.services.tables import change_table_status, create_table
from app.utils.clock import db_now, db_today
from app.utils.ratelimit import login_limiter
from app.utils.security import hash_password, needs_rehash, verify_password
from config import BASE_DIR, ConfigError, TestingConfig
from tests.conftest import make_menu_item, make_user

PASSWORD = "correct-horse-1"


@pytest.fixture(autouse=True)
def _clean_login_limiter():
    """The limiter is process-global; several tests here fail logins on purpose."""
    login_limiter._attempts.clear()
    yield
    login_limiter._attempts.clear()


def req(client, method, url, **kw):
    """Each request as its own identity (known issue #29)."""
    g.pop("current_user", None)
    return getattr(client, method)(url, **kw)


def login(client, username, password=PASSWORD):
    return req(client, "post", "/login", data={"username": username, "password": password})


def user(username, role=Role.CUSTOMER):
    return make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)


def audit_rows(event: AuditEvent) -> list[AuditLog]:
    _db.session.expire_all()
    return _db.session.query(AuditLog).filter_by(event_type=event).all()


def fresh(model, pk):
    _db.session.expire_all()
    return _db.session.get(model, pk)


# =============================================================================
# M2-1  ProxyFix
# =============================================================================


def _proxy_app(monkeypatch, hops: int):
    monkeypatch.setattr(TestingConfig, "TRUSTED_PROXY_COUNT", hops)
    app = create_app("testing")

    @app.get("/_test/request")
    def _request_view():
        return jsonify(remote_addr=request.remote_addr, scheme=request.scheme, host=request.host,
                       secure=request.is_secure, external=url_for("auth.login", _external=True))

    return app


SPOOF = {"X-Forwarded-For": "6.6.6.6", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"}


def test_proxy_default_trusts_no_forwarded_header(monkeypatch):
    seen = _proxy_app(monkeypatch, 0).test_client().get("/_test/request", headers=SPOOF).get_json()
    assert seen == {"remote_addr": "127.0.0.1", "scheme": "http", "host": "localhost", "secure": False,
                    "external": "http://localhost/login"}


def test_proxy_default_audit_ip_is_the_socket_not_the_header(app, client, db):
    user("alice")
    req(client, "post", "/login", data={"username": "alice", "password": "wrong-pass-123"},
        headers={"X-Forwarded-For": "6.6.6.6"})
    assert [r.ip_address for r in audit_rows(AuditEvent.LOGIN_FAILURE)] == ["127.0.0.1"]


def test_proxy_one_trusted_hop_uses_the_proxys_entries(monkeypatch):
    client = _proxy_app(monkeypatch, 1).test_client()
    # The client prepends a spoofed address; the proxy appends the real one.
    seen = client.get("/_test/request", headers={
        "X-Forwarded-For": "6.6.6.6, 203.0.113.9", "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "orders.example.com"}).get_json()
    assert seen["remote_addr"] == "203.0.113.9"
    assert (seen["scheme"], seen["secure"], seen["host"]) == ("https", True, "orders.example.com")
    assert seen["external"] == "https://orders.example.com/login"


def test_proxy_two_trusted_hops_takes_the_second_from_the_right(monkeypatch):
    client = _proxy_app(monkeypatch, 2).test_client()
    seen = client.get("/_test/request", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 10.0.0.2"}).get_json()
    assert seen["remote_addr"] == "198.51.100.7"


def test_proxy_without_forwarded_headers_still_works_when_trusted(monkeypatch):
    seen = _proxy_app(monkeypatch, 1).test_client().get("/_test/request").get_json()
    assert (seen["remote_addr"], seen["scheme"]) == ("127.0.0.1", "http")


def test_proxy_count_must_not_be_negative(monkeypatch):
    monkeypatch.setattr(TestingConfig, "TRUSTED_PROXY_COUNT", -1)
    with pytest.raises(ConfigError, match="TRUSTED_PROXY_COUNT"):
        create_app("testing")


def _config_defaults(*names: str) -> list:
    """Read config class defaults in a clean interpreter with the relevant
    variables unset, so neither this process's env nor .env can mask them."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("TRUSTED_PROXY_COUNT", "LLM_ENABLED", "RESERVATION_ACTIVE_LIMIT")}
    code = ("import json, config; print(json.dumps([[getattr(c, n) for n in %r] for c in "
            "(config.BaseConfig, config.DevelopmentConfig, config.ProductionConfig)]))" % (names,))
    out = subprocess.run([sys.executable, "-c", code], cwd=BASE_DIR, env=env, capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_secure_defaults_are_explicit_in_every_environment():
    for row in _config_defaults("TRUSTED_PROXY_COUNT", "LLM_ENABLED", "RESERVATION_ACTIVE_LIMIT"):
        assert row == [0, False, 2]


# =============================================================================
# M2-2 / M2-3  customer checkout boundaries
# =============================================================================


@pytest.fixture
def shop(db):
    item = make_menu_item(price="100.00")
    table = create_table("T01", 4)
    return item, table


def _cart_and_checkout(client, item, **form):
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    return req(client, "post", "/checkout", data=form)


def _assert_no_order_and_table(table_id, status):
    assert _db.session.query(Order).count() == 0
    assert fresh(RestaurantTable, table_id).status == status


def test_customer_cannot_choose_dine_in(client, shop):
    item, table = shop
    user("cust")
    login(client, "cust")
    _cart_and_checkout(client, item, source="dine_in", table_id=str(table.id))
    _assert_no_order_and_table(table.id, TableStatus.AVAILABLE)
    assert len(audit_rows(AuditEvent.ORDER_CREATION_FAILED)) == 1


def test_customer_cannot_dine_in_without_a_table_either(client, shop):
    item, table = shop
    user("cust")
    login(client, "cust")
    _cart_and_checkout(client, item, source="dine_in")
    _assert_no_order_and_table(table.id, TableStatus.AVAILABLE)


@pytest.mark.parametrize("source", ["takeaway", "online", ""])
def test_customer_cannot_submit_a_table_id_with_any_source(client, shop, source):
    item, table = shop
    user("cust")
    login(client, "cust")
    _cart_and_checkout(client, item, source=source, table_id=str(table.id))
    _assert_no_order_and_table(table.id, TableStatus.AVAILABLE)


def test_customer_cannot_turn_a_reserved_table_occupied(client, shop):
    item, table = shop
    change_table_status(table, "reserved")
    user("cust")
    login(client, "cust")
    _cart_and_checkout(client, item, source="dine_in", table_id=str(table.id))
    _cart_and_checkout(client, item, source="online", table_id=str(table.id))
    _assert_no_order_and_table(table.id, TableStatus.RESERVED)


@pytest.mark.parametrize("table_id", ["999999", "abc", "-1", "1 OR 1=1"])
def test_customer_invalid_or_unknown_table_is_refused(client, shop, table_id):
    item, table = shop
    user("cust")
    login(client, "cust")
    _cart_and_checkout(client, item, source="dine_in", table_id=table_id)
    _assert_no_order_and_table(table.id, TableStatus.AVAILABLE)


def test_customer_cannot_order_delivery_in_the_pilot(client, shop):
    item, table = shop
    user("cust")
    login(client, "cust")
    _cart_and_checkout(client, item, source="delivery")
    _assert_no_order_and_table(table.id, TableStatus.AVAILABLE)


@pytest.mark.parametrize("source", ["takeaway", "online"])
def test_customer_takeaway_and_online_still_work(client, shop, source):
    item, table = shop
    cust = user("cust")
    login(client, "cust")
    assert _cart_and_checkout(client, item, source=source).status_code == 302
    order = _db.session.query(Order).one()
    assert (order.user_id, order.source, order.table_id) == (cust.id, OrderSource(source), None)
    assert fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE
    [row] = audit_rows(AuditEvent.ORDER_CREATED)
    assert json.loads(row.metadata_json)["order_id"] == order.id


def test_checkout_page_offers_no_table_and_no_dine_in(client, shop):
    item, _ = shop
    user("cust")
    login(client, "cust")
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    page = req(client, "get", "/checkout").get_data(as_text=True)
    assert 'name="table_id"' not in page and 'value="dine_in"' not in page and 'value="delivery"' not in page
    assert 'value="takeaway"' in page and 'value="online"' in page


@pytest.mark.parametrize("role", [Role.STAFF, Role.ADMIN])
def test_staff_and_admin_cannot_use_customer_checkout(client, shop, role):
    item, table = shop
    user("worker", role=role)
    login(client, "worker")
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    assert req(client, "get", "/checkout").status_code == 403
    for form in ({"source": "online"}, {"source": "dine_in", "table_id": str(table.id)}):
        assert req(client, "post", "/checkout", data=form).status_code == 403
    _assert_no_order_and_table(table.id, TableStatus.AVAILABLE)


def test_unauthenticated_checkout_is_refused(client, shop):
    item, table = shop
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    assert req(client, "post", "/checkout", data={"source": "online"}).status_code == 401
    browser = req(client, "post", "/checkout", data={"source": "online"}, headers={"Accept": "text/html"})
    assert browser.status_code == 302 and "/login" in browser.headers["Location"]
    _assert_no_order_and_table(table.id, TableStatus.AVAILABLE)


@pytest.mark.parametrize("role", [Role.STAFF, Role.ADMIN])
def test_customer_checkout_service_rejects_non_customers(shop, role):
    item, _ = shop
    worker = user("worker", role=role)
    with pytest.raises(CheckoutError):
        checkout_as_customer(worker, {str(item.id): 1}, "online")
    assert _db.session.query(Order).count() == 0


def test_staff_side_dine_in_still_seats_the_table(shop):
    """The staff-side primitive keeps dine-in: same pricing, table OCCUPIED,
    and released to CLEANING when the order finishes."""
    item, table = shop
    cust, cook = user("cust"), user("cook", role=Role.STAFF)
    order = checkout(cust.id, {str(item.id): 2}, OrderSource.DINE_IN, table.id)
    assert (order.source, order.table_id) == (OrderSource.DINE_IN, table.id)
    assert fresh(RestaurantTable, table.id).status == TableStatus.OCCUPIED
    for status in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.SERVED,
                   OrderStatus.COMPLETED):
        update_order_status(order, status, actor=cook)
    assert fresh(RestaurantTable, table.id).status == TableStatus.CLEANING


# =============================================================================
# M2-4  reservation cap
# =============================================================================


@pytest.fixture
def tables(db):
    return [create_table(f"R{i}", 4) for i in range(1, 6)]


def _day(n=1):
    return (db_today() + timedelta(days=n)).isoformat()


def _book(owner, table, n=1, slot="19:00"):
    return create_reservation(owner, table.id, _day(n), slot, 2)


def test_two_bookings_allowed_third_rejected(tables):
    cust = user("cust")
    _book(cust, tables[0])
    _book(cust, tables[1])
    with pytest.raises(ReservationError) as exc:
        _book(cust, tables[2])
    assert exc.value.errors == {"reservation": [
        "You already have 2 upcoming reservations. Cancel one before booking another."]}
    assert _db.session.query(Reservation).filter_by(user_id=cust.id).count() == 2


def test_cancelling_frees_a_slot(tables):
    cust = user("cust")
    first = _book(cust, tables[0])
    _book(cust, tables[1])
    cancel_reservation_as_customer(first, cust)
    _book(cust, tables[2])
    assert _db.session.query(Reservation).filter_by(user_id=cust.id, status=ReservationStatus.CONFIRMED).count() == 2


@pytest.mark.parametrize("status", [ReservationStatus.COMPLETED, ReservationStatus.NO_SHOW])
def test_finished_reservations_do_not_count(tables, status):
    cust = user("cust")
    done = _book(cust, tables[0])
    done.status = status
    _db.session.commit()
    _book(cust, tables[1])
    _book(cust, tables[2])


def test_past_confirmed_reservations_do_not_count(tables):
    cust = user("cust")
    now = db_now()
    for i, (day, slot) in enumerate([(now.date() - timedelta(days=1), time(19)),
                                     (now.date() - timedelta(days=3), time(13))]):
        _db.session.add(Reservation(user_id=cust.id, table_id=tables[i].id, reservation_date=day,
                                    reservation_time=slot, guest_count=2, status=ReservationStatus.CONFIRMED,
                                    holds_slot=True))
    _db.session.commit()
    _book(cust, tables[2])
    _book(cust, tables[3])
    with pytest.raises(ReservationError):
        _book(cust, tables[4])


def test_customers_have_independent_limits(tables):
    alice, bob = user("alice"), user("bob")
    _book(alice, tables[0])
    _book(alice, tables[1])
    _book(bob, tables[2])
    _book(bob, tables[3])
    with pytest.raises(ReservationError):
        _book(alice, tables[4])


def test_limit_is_configurable(app, tables):
    app.config["RESERVATION_ACTIVE_LIMIT"] = 3
    cust = user("cust")
    for table in tables[:3]:
        _book(cust, table)
    with pytest.raises(ReservationError) as exc:
        _book(cust, tables[3])
    assert "3 upcoming" in exc.value.errors["reservation"][0]


def test_limit_enforced_over_http(client, tables):
    cust = user("cust")
    login(client, "cust")
    for table in tables[:3]:
        req(client, "post", "/reservations", data={"table_id": table.id, "reservation_date": _day(),
                                                   "reservation_time": "19:00", "guest_count": 2})
    assert _db.session.query(Reservation).filter_by(user_id=cust.id).count() == 2
    assert len(audit_rows(AuditEvent.RESERVATION_CREATED)) == 2
    rejected = req(client, "post", "/reservations", headers={"Accept": "application/json"},
                   data={"table_id": tables[3].id, "reservation_date": _day(), "reservation_time": "19:00",
                         "guest_count": 2})
    assert rejected.status_code == 409 and "upcoming reservations" in rejected.get_json()["error"]["message"]


def test_limit_must_be_positive(monkeypatch):
    monkeypatch.setattr(TestingConfig, "RESERVATION_ACTIVE_LIMIT", 0)
    with pytest.raises(ConfigError, match="RESERVATION_ACTIVE_LIMIT"):
        create_app("testing")


# =============================================================================
# M2-5  business change + audit row in one transaction
# =============================================================================


def _boom(*_args):
    raise RuntimeError("audit store unavailable")


def _db_level_audit_failure(*_args):
    """A real database failure for the audit row: NOT NULL event_type."""
    entry = AuditLog(event_type=None, success=True)
    _db.session.add(entry)
    return entry


@pytest.fixture
def placed(db):
    item = make_menu_item(price="100.00")
    cust, cook = user("cust"), user("cook", role=Role.STAFF)
    return checkout(cust.id, {str(item.id): 1}, OrderSource.ONLINE), cust, cook


def test_status_change_and_audit_commit_together(client, placed):
    order, _, _ = placed
    login(client, "cook")
    req(client, "post", f"/staff/orders/{order.id}/status", data={"status": "confirmed"})
    assert fresh(Order, order.id).status == OrderStatus.CONFIRMED
    [row] = audit_rows(AuditEvent.ORDER_STATUS_CHANGED)
    assert json.loads(row.metadata_json) == {"order_id": order.id, "from": "pending", "to": "confirmed",
                                             "actor": "staff"}


def test_status_change_rolls_back_when_the_audit_write_fails(client, placed, monkeypatch):
    order, _, _ = placed
    login(client, "cook")
    monkeypatch.setattr("app.routes.staff_orders.record_event", _boom)
    assert req(client, "post", f"/staff/orders/{order.id}/status", data={"status": "confirmed"}).status_code == 500
    assert fresh(Order, order.id).status == OrderStatus.PENDING
    assert audit_rows(AuditEvent.ORDER_STATUS_CHANGED) == []


@pytest.mark.parametrize("failure", [_boom, _db_level_audit_failure])
def test_order_transition_service_is_atomic_with_its_audit(placed, failure):
    order, _, cook = placed
    with pytest.raises(Exception):
        update_order_status(order, OrderStatus.CONFIRMED, actor=cook, audit=failure)
    assert fresh(Order, order.id).status == OrderStatus.PENDING
    assert _db.session.query(AuditLog).count() == 0


def test_checkout_rolls_back_order_and_table_when_audit_fails(db):
    item, table = make_menu_item(), create_table("T09", 4)
    cust = user("cust")
    with pytest.raises(RuntimeError):
        checkout(cust.id, {str(item.id): 1}, OrderSource.DINE_IN, table.id, audit=_boom)
    assert _db.session.query(Order).count() == 0
    assert fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


def test_customer_checkout_rolls_back_when_audit_fails(client, db, monkeypatch):
    item = make_menu_item()
    user("cust")
    login(client, "cust")
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    monkeypatch.setattr("app.routes.orders.record_event", _boom)
    assert req(client, "post", "/checkout", data={"source": "online"}).status_code == 500
    assert _db.session.query(Order).count() == 0


def test_reservation_create_and_cancel_are_atomic_with_audit(tables):
    cust = user("cust")
    with pytest.raises(RuntimeError):
        create_reservation(cust, tables[0].id, _day(), "19:00", 2, audit=_boom)
    assert _db.session.query(Reservation).count() == 0

    booking = _book(cust, tables[0])
    with pytest.raises(RuntimeError):
        cancel_reservation_as_customer(booking, cust, audit=_boom)
    assert fresh(Reservation, booking.id).status == ReservationStatus.CONFIRMED


def test_failed_business_change_leaves_no_audit_row(tables):
    alice, bob = user("alice"), user("bob")
    _book(alice, tables[0])
    calls = []
    with pytest.raises(ReservationError):  # same table and slot: the unique index refuses it
        create_reservation(bob, tables[0].id, _day(), "19:00", 2,
                           audit=lambda r: calls.append(r) or record_event(AuditEvent.RESERVATION_CREATED,
                                                                           success=True, user_id=bob.id))
    assert _db.session.query(Reservation).filter_by(user_id=bob.id).count() == 0
    assert audit_rows(AuditEvent.RESERVATION_CREATED) == []


def test_table_status_change_is_atomic_with_audit(db):
    table = create_table("T05", 4)
    with pytest.raises(RuntimeError):
        change_table_status(table, "occupied", audit=_boom)
    assert fresh(RestaurantTable, table.id).status == TableStatus.AVAILABLE


def test_staff_approval_rolls_back_when_audit_fails(client, db, monkeypatch):
    user("boss", role=Role.ADMIN)
    pending = make_user(username="newcook", email="newcook@example.com", role=Role.STAFF, staff_approved=False)
    login(client, "boss")
    monkeypatch.setattr("app.routes.admin_staff.record_event", _boom)
    assert req(client, "post", f"/admin/staff/{pending.id}/approve").status_code == 500
    assert fresh(User, pending.id).staff_approved is False


def test_registration_rolls_back_when_audit_fails(client, db, monkeypatch):
    monkeypatch.setattr("app.routes.auth.record_event", _boom)
    req(client, "post", "/register", data={"username": "newbie", "email": "newbie@example.com",
                                           "password": PASSWORD, "password_confirm": PASSWORD})
    assert _db.session.query(User).filter_by(username="newbie").count() == 0


def test_record_event_does_not_commit_on_its_own(db):
    record_event(AuditEvent.LOGIN_FAILURE, success=False)
    _db.session.rollback()
    assert _db.session.query(AuditLog).count() == 0
    record_event(AuditEvent.LOGIN_FAILURE, success=False, commit=True)
    _db.session.rollback()
    assert _db.session.query(AuditLog).count() == 1


def test_independent_security_events_are_still_recorded(client, placed):
    order, _, _ = placed
    login(client, "cust", "wrong-pass-123")
    login(client, "cust")
    req(client, "post", "/logout")
    login(client, "cook")
    req(client, "post", f"/staff/orders/{order.id}/status", data={"status": "completed"})  # illegal jump
    for event in (AuditEvent.LOGIN_FAILURE, AuditEvent.LOGIN_SUCCESS, AuditEvent.LOGOUT,
                  AuditEvent.ORDER_STATUS_CHANGE_REJECTED):
        assert len(audit_rows(event)) >= 1, event
    assert fresh(Order, order.id).status == OrderStatus.PENDING


# =============================================================================
# M2-6  probe endpoints removed
# =============================================================================


@pytest.mark.parametrize("path", ["/account/me", "/account/staff", "/account/admin"])
def test_probe_endpoints_no_longer_exist(path):
    app = create_app("testing")  # the production URL map, without the test-only probes
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert path not in rules and not any(r.startswith("/_test") for r in rules)
    with app.app_context():
        _db.create_all()
        client = app.test_client()
        assert client.get(path).status_code == 404
        user("alice")
        login(client, "alice")
        assert req(client, "get", path).status_code == 404
        _db.session.remove()
        _db.drop_all()


# =============================================================================
# M2-7  Argon2 rehash on login
# =============================================================================

_WEAK = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


def _user_with_old_hash(username="legacy"):
    account = user(username)
    account.password_hash = _WEAK.hash(PASSWORD)
    _db.session.commit()
    return account


def test_old_hash_is_detected_and_still_verifies(db):
    old = _WEAK.hash(PASSWORD)
    assert needs_rehash(old) and verify_password(old, PASSWORD)
    assert not needs_rehash(hash_password(PASSWORD))


def test_successful_login_replaces_an_outdated_hash(client, db):
    account = _user_with_old_hash()
    old = account.password_hash
    assert login(client, "legacy").status_code == 302
    new = fresh(User, account.id).password_hash
    assert new != old and not needs_rehash(new) and verify_password(new, PASSWORD)
    assert login(client, "legacy").status_code == 302  # signs in again with the new hash


def test_current_hash_is_not_rewritten(db):
    account = user("modern")
    before = account.password_hash
    assert authenticate_user("modern", PASSWORD) is not None
    assert fresh(User, account.id).password_hash == before


def test_failed_login_never_rehashes(client, db):
    account = _user_with_old_hash()
    old = account.password_hash
    assert authenticate_user("legacy", "wrong-pass-123") is None
    login(client, "legacy", "wrong-pass-123")
    assert fresh(User, account.id).password_hash == old


def test_inactive_account_is_not_rehashed(db):
    account = _user_with_old_hash()
    account.is_active = False
    _db.session.commit()
    assert authenticate_user("legacy", PASSWORD) is None
    assert needs_rehash(fresh(User, account.id).password_hash)


def test_rehash_never_logs_password_material(client, db, caplog):
    account = _user_with_old_hash()
    old = account.password_hash
    caplog.set_level("DEBUG")
    login(client, "legacy")
    new = fresh(User, account.id).password_hash
    for secret in (PASSWORD, old, new):
        assert secret not in caplog.text
    assert all(secret not in (r.metadata_json or "") for r in _db.session.query(AuditLog) for secret in (old, new))


# =============================================================================
# M2-8  failed-login audit data
# =============================================================================


def _failed_login_metadata(client, username) -> list[dict]:
    req(client, "post", "/login", data={"username": username, "password": "Wrong-Passw0rd!"})
    return [json.loads(r.metadata_json) for r in audit_rows(AuditEvent.LOGIN_FAILURE)]


def test_failed_login_stores_a_keyed_identifier_not_the_username(client, db):
    user("alice")
    [meta] = _failed_login_metadata(client, "alice")
    assert meta == {"login_id": login_identifier("alice")}
    assert len(meta["login_id"]) == 32 and int(meta["login_id"], 16) >= 0
    raw = audit_rows(AuditEvent.LOGIN_FAILURE)[0].metadata_json
    assert "alice" not in raw and "Wrong-Passw0rd" not in raw


@pytest.mark.parametrize("typed", [
    "<script>alert(1)</script>",
    "' OR 1=1; --",
    "Hunter2-Secret-Pw!",      # a password typed into the username box
    "x" * 32,                  # the longest the form accepts
    "名前‮\x00",
])
def test_attacker_controlled_login_input_is_never_stored(client, db, typed):
    rows = _failed_login_metadata(client, typed)
    stored = "".join(r.metadata_json or "" for r in _db.session.query(AuditLog))
    assert typed.strip().lower() not in stored.lower() and typed not in stored
    for meta in rows:
        assert set(meta) == {"login_id"} and len(meta["login_id"]) == 32


def test_overlong_login_input_is_bounded(client, db):
    assert len(login_identifier("a" * 100_000)) == 32
    req(client, "post", "/login", data={"username": "a" * 5000, "password": "whatever-123"})
    assert all(len(r.metadata_json or "") <= 64 for r in _db.session.query(AuditLog))


def test_repeated_attempts_on_one_name_correlate(client, db):
    for typed in ("Alice", " alice ", "ALICE"):
        req(client, "post", "/login", data={"username": typed, "password": "Wrong-Passw0rd!"})
    ids = {json.loads(r.metadata_json)["login_id"] for r in audit_rows(AuditEvent.LOGIN_FAILURE)}
    assert ids == {login_identifier("alice")}
    assert login_identifier("alice") != login_identifier("bob")


def test_rate_limited_event_uses_the_identifier_too(client, db):
    for _ in range(12):
        req(client, "post", "/login", data={"username": "target", "password": "Wrong-Passw0rd!"})
    limited = audit_rows(AuditEvent.LOGIN_RATE_LIMITED)
    assert limited and all(json.loads(r.metadata_json) == {"login_id": login_identifier("target")} for r in limited)


def test_identifier_is_keyed_with_the_secret(app):
    first = login_identifier("alice")
    app.config["SECRET_KEY"] = "another-secret-for-this-test-only"
    assert login_identifier("alice") != first


# =============================================================================
# M2-9  LLM disabled by default
# =============================================================================


def test_llm_off_by_default_and_outside_an_app():
    assert TestingConfig.LLM_ENABLED is False
    assert local_llm.is_enabled() is False  # no app context: fail closed


def test_disabled_llm_never_loads_or_runs(app, monkeypatch):
    def forbidden():
        raise AssertionError("model load attempted while disabled")

    monkeypatch.setattr(local_llm, "_load", forbidden)
    monkeypatch.setattr(local_llm, "_warmup_started", False)
    app.config.update(LLM_ENABLED=False, TESTING=False, LLM_WARMUP=True)
    request_ = local_llm.ExplanationRequest(item_name="Dal", item_cuisine="indian", item_dietary="vegan",
                                            item_spice="mild", preferred_cuisine=None, preferred_dietary=None,
                                            preferred_spice=None, match_label="Good match")
    assert local_llm.generate_explanation(request_) is None
    assert local_llm.warm_up(app) is False


def test_opt_in_still_reaches_the_model(app, monkeypatch):
    calls = []
    monkeypatch.setattr(local_llm, "_load", lambda: calls.append(1) or (None, None))
    app.config["LLM_ENABLED"] = True
    request_ = local_llm.ExplanationRequest(item_name="Dal", item_cuisine="indian", item_dietary="vegan",
                                            item_spice="mild", preferred_cuisine=None, preferred_dietary=None,
                                            preferred_spice=None, match_label="Good match")
    assert local_llm.generate_explanation(request_) is None  # model absent here: degrades, never raises
    assert calls == [1]


def test_core_flow_and_explain_page_work_with_llm_disabled(client, db, monkeypatch):
    monkeypatch.setattr(local_llm, "_load", lambda: (_ for _ in ()).throw(AssertionError("loaded")))
    item = make_menu_item()
    user("cust")
    login(client, "cust")
    assert req(client, "get", "/menu").status_code == 200
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    assert req(client, "post", "/checkout", data={"source": "takeaway"}).status_code == 302
    assert req(client, "get", "/recommendations/explain").status_code == 200
    assert _db.session.query(Order).count() == 1
