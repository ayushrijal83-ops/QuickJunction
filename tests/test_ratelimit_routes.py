"""S1-B: the shared rate limiter, wired into the application's routes.

Runs on the in-memory path (no REDIS_URL), like the rest of the suite; the
shared-store and outage behaviour at the routes is driven through the same
stand-in store tests/test_ratelimit.py uses. The same routes against a real
Redis server are in tests/test_ratelimit_redis.py.

Policies (config.py RATE_LIMITS): login 5 failures / 15 min per (IP,
normalized username); password change 5 wrong current passwords / 15 min per
account; registration 10 / 60 min per IP; account setup 10 / 15 min per IP;
checkout 8 / 5 min, reservations 10 / 5 min, AI explanation 4 / min, each per
authenticated customer.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from flask import g

import app.utils.ratelimit as ratelimit
from app import create_app
from app.extensions import db as _db
from app.models.account_token import AccountToken
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import Order, OrderItem
from app.models.payment import Payment
from app.models.payment_ledger import PaymentLedgerEntry
from app.models.reservation import Reservation
from app.models.stock_movement import StockMovement
from app.models.user import Role, User
from app.services.accounts import invite_staff, issue_password_reset
from app.services.tables import create_table
from app.utils.clock import db_today
from app.utils.ratelimit import (
    POLICIES,
    FailureRateLimiter,
    SharedBackend,
    account_setup_limiter,
    ai_explanation_limiter,
    checkout_limiter,
    login_limiter,
    password_change_limiter,
    rate_limit_status,
    registration_limiter,
    reservation_limiter,
)
from app.utils.security import verify_password
from config import BaseConfig, ConfigError, TestingConfig
from tests.conftest import make_category, make_menu_item, make_user
from tests.test_ratelimit import Clock, StandInStore, security_log  # noqa: F401 (a fixture)

PASSWORD = "correct-horse-1"
NEW_PASSWORD = "brand-new-pass-42"
OTHER_IP = "198.51.100.7"


def req(client, method, url, *, ip=None, **kw):
    """One request as its own identity (known issue #29: ``g`` survives
    between test-client requests), optionally from another client IP."""
    g.pop("current_user", None)
    if ip:
        kw["environ_base"] = {**kw.get("environ_base", {}), "REMOTE_ADDR": ip}
    return getattr(client, method)(url, **kw)


def user(username, role=Role.CUSTOMER, **kw):
    return make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role, **kw)


def login(client, username, password=PASSWORD, portal="", **kw):
    return req(client, "post", f"/login/{portal}" if portal else "/login",
               data={"username": username, "password": password}, **kw)


def signed_in(app, username, role=Role.CUSTOMER):
    account = user(username, role)
    client = app.test_client()
    assert login(client, username).status_code == 302
    login_limiter._attempts.clear()  # signing in is not what these tests count
    return client, account


def retry_after(response) -> int:
    value = int(response.headers["Retry-After"])
    assert value >= 1
    return value


def audit_count(event=None) -> int:
    _db.session.expire_all()
    query = _db.session.query(AuditLog)
    return (query.filter_by(event_type=event) if event else query).count()


def test_every_route_policy_matches_the_approved_table():
    assert {name: (l.limit, l.window_seconds) for name, l in POLICIES.items()} == {
        "login": (5, 900), "password_change": (5, 900), "registration": (10, 3600),
        "account_setup": (10, 900), "checkout": (8, 300), "reservation": (10, 300), "ai_explanation": (4, 60),
    }
    assert BaseConfig.RATE_LIMITS == {name: (l.limit, l.window_seconds) for name, l in POLICIES.items()}
    assert isinstance(login_limiter, FailureRateLimiter) and isinstance(password_change_limiter, FailureRateLimiter)
    assert all(l._backend is ratelimit._default_backend for l in POLICIES.values())


# =============================================================================
# login
# =============================================================================


def test_login_allows_five_failures_then_refuses_with_retry_after(client, db):
    user("alice")
    for _ in range(5):
        response = login(client, "alice", "wrong-password-1")
        assert response.status_code == 200 and "Invalid username or password." in response.get_data(as_text=True)
    refused = login(client, "alice", PASSWORD)  # even the right password: not checked at all
    assert refused.status_code == 429
    assert "Too many login attempts" in refused.get_data(as_text=True)
    assert 1 <= retry_after(refused) <= 900
    assert audit_count(AuditEvent.LOGIN_FAILURE) == 5
    assert audit_count(AuditEvent.LOGIN_RATE_LIMITED) == 1
    assert audit_count(AuditEvent.LOGIN_SUCCESS) == 0


def test_successful_login_resets_failures_and_does_not_consume_a_slot(client, db):
    user("alice")
    for _ in range(4):
        login(client, "alice", "wrong-password-1")
    assert login(client, "alice").status_code == 302  # the fifth, reserved attempt succeeds
    assert login_limiter._attempts == {}  # nothing left behind: not the failures, not the reservation
    req(client, "post", "/logout")
    for _ in range(5):  # a full allowance again
        assert login(client, "alice", "wrong-password-1").status_code == 200
    assert login(client, "alice").status_code == 429


def test_login_key_is_ip_plus_normalized_username(app, client, db):
    user("alice")
    for name in ("alice", " Alice", "ALICE ", "aLiCe", "alice"):
        assert login(client, name, "wrong-password-1").status_code == 200
    assert login(client, "alice").status_code == 429
    assert list(login_limiter._attempts) == ["127.0.0.1:alice"]
    assert login(client, "bob", "wrong-password-1").status_code == 200  # another name has its own count
    assert login(app.test_client(), "alice", ip=OTHER_IP).status_code == 302  # so does another IP


def test_an_attempt_in_flight_holds_its_slot(app, client, db, monkeypatch):
    """The old check-then-record let every request that passed the check
    before the fifth failure was recorded go on to guess. Now the attempt is
    counted before the password is checked, so a concurrent sixth is refused
    while the fifth is still being verified."""
    import app.routes.auth as auth_routes

    user("alice")
    for _ in range(4):
        login(client, "alice", "wrong-password-1")
    real = auth_routes.authenticate_user
    seen = {}

    def slow_check(username, password):
        if "concurrent" not in seen:  # while this (fifth) attempt is in flight...
            seen["concurrent"] = login(app.test_client(), "alice", PASSWORD).status_code
        return real(username, password)

    monkeypatch.setattr(auth_routes, "authenticate_user", slow_check)
    assert login(client, "alice", "wrong-password-1").status_code == 200
    assert seen["concurrent"] == 429  # ...the sixth is refused unchecked, even with the right password
    assert len(login_limiter._attempts["127.0.0.1:alice"]) == 5  # five guesses, never more


def test_a_login_that_errors_releases_its_reservation(client, db, monkeypatch):
    import app.routes.auth as auth_routes

    user("alice")
    login(client, "alice", "wrong-password-1")

    def broken(username, password):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(auth_routes, "authenticate_user", broken)
    assert login(client, "alice").status_code == 500
    assert len(login_limiter._attempts["127.0.0.1:alice"]) == 1  # the error was not counted as a failure


def test_an_unresolved_reservation_expires_like_any_failure(monkeypatch):
    """A worker that dies between reserving and resolving leaves one entry
    behind; it leaves the window on schedule, so nobody is locked out."""
    clock = Clock()
    monkeypatch.setattr(ratelimit, "_now", clock)
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    for _ in range(5):
        assert limiter.try_reserve("203.0.113.9:alice").allowed  # five attempts, none ever resolved
    assert not limiter.try_reserve("203.0.113.9:alice").allowed
    clock.advance(899)
    refused = limiter.try_reserve("203.0.113.9:alice")
    assert not refused.allowed and refused.retry_after == pytest.approx(1)
    clock.advance(1)
    assert limiter.try_reserve("203.0.113.9:alice").allowed


def test_release_withdraws_only_its_own_reservation(monkeypatch):
    monkeypatch.setattr(ratelimit, "_now", Clock())
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    first, second, third = (limiter.try_reserve("k") for _ in range(3))
    limiter.release("k", second.token)
    assert [token for _, token in limiter._attempts["k"]] == [first.token, third.token]
    limiter.release("k", "not-a-token")
    limiter.release("k", None)
    assert len(limiter._attempts["k"]) == 2


def test_staff_and_admin_portals_keep_their_behaviour(client, db):
    user("cook", Role.STAFF)
    user("boss", Role.ADMIN)
    user("pending", Role.STAFF, staff_approved=False)
    for _ in range(4):
        login(client, "cook", "wrong-password-1", "staff")
    # Right password, wrong portal: refused without a session, and -- as before
    # -- not a failed guess, so the key is reset.
    wrong_portal = login(client, "cook", PASSWORD, "admin")
    assert wrong_portal.status_code == 200 and "does not have admin access" in wrong_portal.get_data(as_text=True)
    assert "127.0.0.1:cook" not in login_limiter._attempts
    pending = login(client, "pending", PASSWORD, "staff")
    assert "waiting for administrator approval" in pending.get_data(as_text=True)
    assert "127.0.0.1:pending" not in login_limiter._attempts
    assert login(client, "cook", PASSWORD, "staff").status_code == 302
    req(client, "post", "/logout")
    assert login(client, "boss", PASSWORD, "admin").status_code == 302
    req(client, "post", "/logout")
    for _ in range(5):
        login(client, "boss", "wrong-password-1", "admin")
    assert login(client, "boss", PASSWORD, "admin").status_code == 429  # the admin portal is limited too
    assert login(client, "boss", PASSWORD).status_code == 429  # same key whichever portal


# =============================================================================
# registration
# =============================================================================


def register(client, n, **kw):
    return req(client, "post", "/register", data={
        "username": f"newuser{n}", "email": f"newuser{n}@example.com",
        "password": PASSWORD, "password_confirm": PASSWORD}, **kw)


def test_registration_is_limited_per_ip_and_rotating_names_does_not_help(client, db):
    for n in range(10):
        response = register(client, n)
        assert response.status_code == 302
        req(client, "post", "/logout")  # registering signs you in
    refused = register(client, 10)
    assert refused.status_code == 429
    assert "Too many sign-up attempts" in refused.get_data(as_text=True)
    assert 1 <= retry_after(refused) <= 3600
    assert _db.session.query(User).filter_by(username="newuser10").count() == 0  # nothing created
    assert register(client, 11, ip=OTHER_IP).status_code == 302  # another IP is independent


def test_invalid_registrations_count_too(client, db):
    for n in range(10):
        bad = req(client, "post", "/register", data={"username": f"x{n}", "email": "nope", "password": "a",
                                                      "password_confirm": "b"})
        assert bad.status_code == 200
    assert register(client, 1).status_code == 429


def test_registration_form_page_is_not_counted(client, db):
    for _ in range(15):
        assert req(client, "get", "/register").status_code == 200
    assert register(client, 1).status_code == 302


def test_registration_ignores_a_spoofed_forwarded_for(client, db):
    """TRUSTED_PROXY_COUNT is 0 by default: X-Forwarded-For is not believed,
    so a client cannot give itself a fresh IP per request."""
    for n in range(10):
        register(client, n, headers={"X-Forwarded-For": f"203.0.113.{n}"})
        req(client, "post", "/logout")
    assert register(client, 10, headers={"X-Forwarded-For": "203.0.113.99"}).status_code == 429
    assert list(registration_limiter._local._attempts) == ["127.0.0.1"]


# =============================================================================
# account setup (one-time codes)
# =============================================================================


def redeem(client, code, password=NEW_PASSWORD, **kw):
    return req(client, "post", "/account/setup",
               data={"code": code, "password": password, "password_confirm": password}, **kw)


@pytest.fixture
def boss(db):
    return user("boss", Role.ADMIN)


def test_account_setup_is_limited_per_ip(client, boss):
    _, code = invite_staff(boss, "newcook", "newcook@example.com")
    for n in range(10):
        assert redeem(client, f"guess-{n}").status_code == 200  # wrong codes: rejected, and counted
    refused = redeem(client, code)  # the right code, but over the limit: not even looked at
    assert refused.status_code == 429
    assert 1 <= retry_after(refused) <= 900
    token = _db.session.query(AccountToken).one()
    assert token.used_at is None  # not consumed
    assert _db.session.query(User).filter_by(username="newcook").one().is_active is False
    assert redeem(client, code, ip=OTHER_IP).status_code == 302  # another IP still can
    assert list(account_setup_limiter._local._attempts) == ["127.0.0.1", OTHER_IP]  # IPs, never codes


def test_account_setup_flows_are_unchanged_within_the_limit(app, client, boss):
    cook, invite_code = invite_staff(boss, "newcook", "newcook@example.com")
    assert "invalid or has expired" in redeem(client, "not-a-code").get_data(as_text=True)
    assert redeem(client, invite_code).status_code == 302  # valid invitation
    assert verify_password(_db.session.get(User, cook.id).password_hash, NEW_PASSWORD)
    used = redeem(client, invite_code, password="another-pass-77")  # single use
    assert "invalid or has expired" in used.get_data(as_text=True)

    reset_code = issue_password_reset(boss, _db.session.get(User, cook.id))
    token = _db.session.query(AccountToken).filter(AccountToken.used_at.is_(None)).one()
    token.expires_at = token.created_at - timedelta(seconds=1)
    _db.session.commit()
    assert "invalid or has expired" in redeem(client, reset_code).get_data(as_text=True)  # expired

    fresh_code = issue_password_reset(boss, _db.session.get(User, cook.id))
    assert redeem(client, fresh_code, password="reset-pass-55").status_code == 302  # valid reset
    _db.session.expire_all()
    assert verify_password(_db.session.get(User, cook.id).password_hash, "reset-pass-55")


def test_account_setup_page_view_is_not_counted(client, db):
    for _ in range(12):
        assert req(client, "get", "/account/setup").status_code == 200
    assert redeem(client, "guess").status_code == 200


# =============================================================================
# password change
# =============================================================================


def change(client, current, new=NEW_PASSWORD, **kw):
    return req(client, "post", "/account/password",
               data={"current_password": current, "password": new, "password_confirm": new}, **kw)


def test_password_change_policy_is_preserved(app):
    client, alice = signed_in(app, "alice")
    for _ in range(5):
        response = change(client, "wrong-current-1")
        assert response.status_code == 200 and "current password is incorrect" in response.get_data(as_text=True)
    refused = change(client, PASSWORD)  # right password, over the limit: refused unchecked
    assert refused.status_code == 429 and "Too many attempts" in refused.get_data(as_text=True)
    assert 1 <= retry_after(refused) <= 900
    _db.session.expire_all()
    assert verify_password(_db.session.get(User, alice.id).password_hash, PASSWORD)
    assert list(login_limiter._attempts) == [f"password-change:{alice.id}"]


def test_password_change_is_keyed_by_the_signed_in_account(app):
    client, alice = signed_in(app, "alice")
    other, bob = signed_in(app, "bob")
    for _ in range(5):
        # A forged user id in the form is ignored: the session decides.
        req(client, "post", "/account/password", data={"current_password": "wrong", "password": NEW_PASSWORD,
                                                       "password_confirm": NEW_PASSWORD, "user_id": bob.id})
    assert change(client, PASSWORD).status_code == 429
    assert change(other, PASSWORD).status_code == 302  # bob is unaffected
    assert f"password-change:{bob.id}" not in login_limiter._attempts


def test_password_change_success_and_policy_errors(app):
    client, alice = signed_in(app, "alice")
    for _ in range(4):
        change(client, "wrong-current-1")
    for _ in range(3):  # a new password the policy rejects is not a wrong current password
        response = change(client, PASSWORD, new="short")
        assert response.status_code == 200
    assert len(login_limiter._attempts[f"password-change:{alice.id}"]) == 4
    response = change(client, PASSWORD)
    assert response.status_code == 302  # success...
    assert f"password-change:{alice.id}" not in login_limiter._attempts  # ...resets the count


def test_password_change_that_errors_releases_its_reservation(app, monkeypatch):
    import app.routes.account as account_routes

    client, alice = signed_in(app, "alice")
    change(client, "wrong-current-1")

    def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(account_routes, "change_password", broken)
    assert change(client, PASSWORD).status_code == 500
    assert len(login_limiter._attempts[f"password-change:{alice.id}"]) == 1


# =============================================================================
# checkout
# =============================================================================


def side_effects() -> dict:
    _db.session.expire_all()
    return {model.__name__: _db.session.query(model).count()
            for model in (Order, OrderItem, Payment, PaymentLedgerEntry, StockMovement, AuditLog)}


def test_checkout_is_limited_per_customer_with_no_side_effects(app):
    item = make_menu_item(category=make_category(), price="99.00")
    client, alice = signed_in(app, "alice")
    for _ in range(8):  # empty-cart attempts: rejected by checkout itself, and counted
        assert req(client, "post", "/checkout", data={}).status_code == 302
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 2})
    before = side_effects()

    refused = req(client, "post", "/checkout", data={})
    assert refused.status_code == 429
    assert "placing orders too quickly" in refused.get_data(as_text=True)
    assert 1 <= retry_after(refused) <= 300
    assert side_effects() == before  # no order, item, payment, ledger entry, stock movement or audit row
    with client.session_transaction() as session:
        assert session["cart"] == {str(item.id): 2}  # the cart is kept for later

    other, bob = signed_in(app, "bob")  # another customer's counter is untouched
    req(other, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    assert req(other, "post", "/checkout", data={}).status_code == 302
    assert _db.session.query(Order).filter_by(user_id=bob.id).count() == 1
    assert list(checkout_limiter._local._attempts) == [str(alice.id), str(bob.id)]


def test_checkout_429_is_json_for_api_clients(app):
    client, _ = signed_in(app, "alice")
    for _ in range(8):
        req(client, "post", "/checkout", data={})
    refused = req(client, "post", "/checkout", data={}, headers={"Accept": "application/json"})
    assert refused.status_code == 429
    assert refused.get_json() == {"error": {"status": 429, "message":
                                            "You're placing orders too quickly. Please wait a moment and try again."}}
    assert "Retry-After" in refused.headers


def test_successful_checkout_is_unchanged(app):
    item = make_menu_item(category=make_category(), price="99.00")
    client, alice = signed_in(app, "alice")
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 2})
    response = req(client, "post", "/checkout", data={})
    assert response.status_code == 302
    order = _db.session.query(Order).one()
    assert order.user_id == alice.id and len(order.items) == 1
    assert audit_count(AuditEvent.ORDER_CREATED) == 1


def test_checkout_authorization_runs_first_and_is_not_counted(app, client, db):
    assert req(client, "post", "/checkout", data={}).status_code == 401
    staff, _ = signed_in(app, "cook", Role.STAFF)
    for _ in range(10):
        assert req(staff, "post", "/checkout", data={}).status_code == 403
    assert checkout_limiter._local._attempts == {}


# =============================================================================
# reservations
# =============================================================================


def booking(table, day=None, at="19:00"):
    return {"table_id": table.id, "reservation_date": day or (db_today() + timedelta(days=1)).isoformat(),
            "reservation_time": at, "guest_count": 2}


def test_reservations_are_limited_per_customer_and_429_books_nothing(app):
    table = create_table("T01", 4)
    client, alice = signed_in(app, "alice")
    for _ in range(10):  # incomplete forms: rejected, and counted
        assert req(client, "post", "/reservations", data={}).status_code == 302
    audits = audit_count()
    refused = req(client, "post", "/reservations", data=booking(table))
    assert refused.status_code == 429
    assert "making reservations too quickly" in refused.get_data(as_text=True)
    assert 1 <= retry_after(refused) <= 300
    assert _db.session.query(Reservation).count() == 0
    assert audit_count() == audits

    other, bob = signed_in(app, "bob")
    assert req(other, "post", "/reservations", data=booking(table)).status_code == 302
    assert _db.session.query(Reservation).one().user_id == bob.id
    json_refused = req(client, "post", "/reservations", data=booking(table), headers={"Accept": "application/json"})
    assert json_refused.status_code == 429 and json_refused.get_json()["error"]["status"] == 429


def test_active_reservation_cap_is_separate_and_unchanged(app):
    """RESERVATION_ACTIVE_LIMIT (2) still refuses the third active booking,
    well inside the request rate limit."""
    tables = [create_table(f"T0{n}", 4) for n in range(1, 4)]
    client, alice = signed_in(app, "alice")
    assert req(client, "post", "/reservations", data=booking(tables[0], at="17:00")).status_code == 302
    assert req(client, "post", "/reservations", data=booking(tables[1], at="19:00")).status_code == 302
    third = req(client, "post", "/reservations", data=booking(tables[2], at="21:00"),
                headers={"Accept": "application/json"})
    assert third.status_code == 409  # the business rule, not the rate limit
    assert _db.session.query(Reservation).filter_by(user_id=alice.id).count() == 2
    assert len(reservation_limiter._local._attempts[str(alice.id)]) == 3


# =============================================================================
# AI explanation
# =============================================================================


@pytest.fixture
def inference(monkeypatch):
    calls = []

    def fake_generate(request, **kwargs):
        calls.append(request.item_name)
        return f"{request.item_name} suits you."

    monkeypatch.setattr("app.routes.preferences.generate_explanation", fake_generate)
    return calls


def test_explanation_is_limited_per_customer_before_inference(app, inference):
    make_menu_item(category=make_category(), name="Paneer Tikka", price="249.00")
    client, alice = signed_in(app, "alice")
    for _ in range(4):
        response = req(client, "get", "/recommendations/explain")
        assert response.status_code == 200 and "Paneer Tikka suits you." in response.get_data(as_text=True)
    assert len(inference) == 4
    refused = req(client, "get", "/recommendations/explain")
    assert refused.status_code == 429
    assert "Too many explanation requests" in refused.get_data(as_text=True)
    assert 1 <= retry_after(refused) <= 60
    assert len(inference) == 4  # the model was not invoked

    other, _ = signed_in(app, "bob")
    assert req(other, "get", "/recommendations/explain").status_code == 200
    assert len(inference) == 5
    assert req(client, "get", "/recommendations").status_code == 200  # the main page is not limited


def test_explanation_graceful_degradation_is_unchanged(app, monkeypatch):
    make_menu_item(category=make_category(), name="Paneer Tikka", price="249.00")
    monkeypatch.setattr("app.routes.preferences.generate_explanation", lambda r, **kw: None)
    client, _ = signed_in(app, "alice")
    response = req(client, "get", "/recommendations/explain")
    assert response.status_code == 200 and "Paneer Tikka" in response.get_data(as_text=True)


def test_explanation_requires_a_customer_before_counting(app, client, db):
    assert req(client, "get", "/recommendations/explain").status_code == 401
    staff, _ = signed_in(app, "cook", Role.STAFF)
    assert req(staff, "get", "/recommendations/explain").status_code == 403
    assert ai_explanation_limiter._local._attempts == {}


# =============================================================================
# shared store, outage and recovery, through the routes
# =============================================================================


@pytest.fixture
def store(monkeypatch):
    """The app's backend pointed at a stand-in shared store, as if REDIS_URL
    were set; ``store.down = True`` is an outage."""
    stand_in = StandInStore()
    monkeypatch.setattr(ratelimit._default_backend, "_store", stand_in)
    monkeypatch.setattr(ratelimit._default_backend, "_retry_at", 0.0)
    monkeypatch.setattr(ratelimit._default_backend, "_degraded_since", None)
    monkeypatch.setattr(ratelimit._default_backend, "_last_warning", {})
    return stand_in


def test_routes_count_in_the_shared_store_and_another_worker_sees_it(app, store):
    client, alice = signed_in(app, "alice")
    for _ in range(8):
        req(client, "post", "/checkout", data={})
    assert checkout_limiter._local._attempts == {}  # nothing counted in this process
    second_worker = SharedBackend()
    second_worker._store = store  # a second process, same store
    from app.utils.ratelimit import RequestRateLimiter

    assert not RequestRateLimiter("checkout", 8, 300, backend=second_worker).attempt(str(alice.id)).allowed
    assert req(client, "post", "/checkout", data={}).status_code == 429
    assert rate_limit_status()["shared"] is True


def test_outage_falls_back_to_local_limits_then_recovers(app, store, security_log, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ratelimit, "_now", clock)
    client, alice = signed_in(app, "alice")
    for _ in range(3):
        req(client, "post", "/checkout", data={})  # three in the shared store

    store.down = True
    statuses = [req(client, "post", "/checkout", data={}).status_code for _ in range(9)]
    assert statuses == [302] * 8 + [429]  # protected locally -- a fresh local count, see SECURITY.md 22
    assert rate_limit_status()["degraded"] is True
    limiter_logs = [r for r in security_log if r.getMessage().startswith("rate limiting")]
    assert len(limiter_logs) == 1  # one warning per policy per minute, not one per request
    text = limiter_logs[0].getMessage()
    assert "policy=checkout" in text and "NOT shared across workers" in text
    for leak in ("alice", "127.0.0.1", f"={alice.id} ", "do-not-log-me", "10.0.0.9", "redis://"):
        assert leak not in text

    store.down = False
    clock.advance(SharedBackend.RETRY_SECONDS)
    assert req(client, "post", "/checkout", data={}).status_code == 302  # shared again: 4th in the store
    assert rate_limit_status() == {**rate_limit_status(), "shared": True, "degraded": False}
    assert any("restored" in r.getMessage() for r in security_log)


def test_login_during_an_outage_still_limits_and_success_still_resets(client, db, store):
    user("alice")
    store.down = True
    for _ in range(5):
        assert login(client, "alice", "wrong-password-1").status_code == 200
    assert login(client, "alice").status_code == 429
    store.down = False
    ratelimit._default_backend._retry_at = 0.0
    # The shared store never saw those five (counts do not carry across the
    # switch): the right password now succeeds, and reset clears both stores.
    assert login(client, "alice").status_code == 302
    assert login_limiter._attempts == {}


def test_no_redis_mode_reports_memory(app):
    assert rate_limit_status() == {"backend": "memory", "shared": False, "degraded": False,
                                   "degraded_since": None, "last_error": None}


# =============================================================================
# configuration
# =============================================================================


def test_rate_limits_come_from_the_app_config(monkeypatch):
    monkeypatch.setattr(TestingConfig, "RATE_LIMITS", {**BaseConfig.RATE_LIMITS, "checkout": (2, 30)})
    try:
        create_app("testing")
        assert (checkout_limiter.limit, checkout_limiter.window_seconds) == (2, 30)
    finally:
        monkeypatch.undo()
        create_app("testing")
    assert (checkout_limiter.limit, checkout_limiter.window_seconds) == (8, 300)


@pytest.mark.parametrize("limits", [
    {k: v for k, v in BaseConfig.RATE_LIMITS.items() if k != "login"},
    {**BaseConfig.RATE_LIMITS, "unknown": (1, 1)},
    {**BaseConfig.RATE_LIMITS, "login": (0, 900)},
    {**BaseConfig.RATE_LIMITS, "login": (5, -1)},
    {**BaseConfig.RATE_LIMITS, "login": (5.5, 900)},
    {**BaseConfig.RATE_LIMITS, "login": [5, 900]},
])
def test_invalid_rate_limits_are_refused(monkeypatch, limits):
    monkeypatch.setattr(TestingConfig, "RATE_LIMITS", limits)
    with pytest.raises(ConfigError, match="RATE_LIMITS"):
        TestingConfig.validate()


def test_429_never_carries_internal_details(app):
    client, alice = signed_in(app, "alice")
    for _ in range(8):
        req(client, "post", "/checkout", data={})
    body = json.dumps(req(client, "post", "/checkout", data={}, headers={"Accept": "application/json"}).get_json())
    for secret in ("redis", "qj:rl", "checkout_limiter", str(alice.id) + '"', "127.0.0.1", "count"):
        assert secret not in body.lower()
