"""Milestone 09 regression tests: server-side session revocation.

The vulnerability these guard against, verified live during the M08 audit:

    1. log in
    2. copy the signed `qj_session` cookie
    3. log out
    4. replay the copied cookie  ->  it still authenticated (HTTP 200)

Root cause: the session was a stateless signed cookie holding only a user id,
so `session.clear()` on logout cleared the browser's own copy but had no
server-side state to invalidate. M09 added `users.session_version`, stamped
into the session at login, compared on every request and incremented on
logout.

Every replay test below is paired with a **control** that replays a cookie
which has *not* been revoked and asserts it still works. Without the control
a broken replay harness would make these tests pass for the wrong reason.
"""

from __future__ import annotations

import pytest
from flask import g

from app.extensions import db as _db
from app.models.user import Role, User
from tests.conftest import make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"


def login(client, username: str, password: str = PASSWORD):
    # Clearing first is essential, not cosmetic: /login redirects away when a
    # user is already current, so a stale g cache would silently skip the
    # login and leave the client unauthenticated (see forget_cached_user).
    forget_cached_user()
    response = client.post("/login", data={"username": username, "password": password})
    forget_cached_user()
    return response


def make_and_login(client, username="revcust", role=Role.CUSTOMER):
    user = make_user(username=username, email=f"{username}@example.com",
                     password=PASSWORD, role=role)
    login(client, username)
    return user


def forget_cached_user() -> None:
    """Drop the per-application-context cache of the current user.

    The pytest ``app`` fixture holds **one** application context open for the
    whole test, and Flask reuses an already-pushed app context for test-client
    requests instead of creating one per request. ``g.current_user`` therefore
    survives between requests in a test -- including across different clients
    -- which production never does, because each real request pushes its own
    app context.

    Without clearing it, a test that changes identity mid-test would assert
    against a stale cached object rather than the real session check, and
    would pass for the wrong reason. Called before every assertion below that
    follows a login, logout, or identity change.
    """
    g.pop("current_user", None)


def capture_session(app, client) -> str:
    """The signed cookie value currently held by ``client``.

    This is the attacker's-eye view: a copy of the cookie, taken while it is
    valid, replayable from anywhere.
    """
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    assert cookie is not None, "expected an authenticated session cookie"
    return cookie.value


def replay(app, captured: str, path: str = "/orders") -> int:
    """Replay a captured cookie from a *fresh* client, as an attacker would."""
    forget_cached_user()
    attacker = app.test_client()
    attacker.set_cookie(app.config["SESSION_COOKIE_NAME"], captured)
    status = attacker.get(path).status_code
    forget_cached_user()
    return status


# --- 1-3. The vulnerability itself -------------------------------------------


def test_1_logout_invalidates_the_current_session(app, client, db):
    make_and_login(client, "logout_current")
    assert client.get("/orders").status_code == 200

    client.post("/logout", data={})
    assert client.get("/orders").status_code == 401


def test_2_copied_cookie_cannot_authenticate_after_logout(app, client, db):
    """The M08 vulnerability, expressed directly."""
    make_and_login(client, "replay_victim")
    captured = capture_session(app, client)

    # CONTROL: the captured cookie is genuinely usable before logout, so a
    # 401 afterwards can only be caused by revocation.
    assert replay(app, captured) == 200, "replay harness is broken; test would be vacuous"

    client.post("/logout", data={})

    assert replay(app, captured) == 401, "copied cookie still authenticates after logout"
    # Also across other protected surfaces, not just /orders.
    assert replay(app, captured, "/account/me") == 401
    assert replay(app, captured, "/preferences") == 401


def test_3_fresh_login_works_after_logout(app, client, db):
    user = make_and_login(client, "fresh_login")
    client.post("/logout", data={})
    assert client.get("/orders").status_code == 401

    login(client, "fresh_login")
    assert client.get("/orders").status_code == 200
    assert client.get("/account/me").get_json()["username"] == "fresh_login"

    # The new session carries the incremented version.
    _db.session.refresh(user)
    assert user.session_version == 1


# --- 4. Unauthenticated access ------------------------------------------------


def test_4_unauthenticated_request_is_still_401(client, db):
    assert client.get("/orders").status_code == 401
    assert client.get("/account/me").status_code == 401
    assert client.get("/preferences").status_code == 401
    assert client.get("/staff/orders").status_code == 401


def test_4b_a_forged_or_garbage_cookie_never_authenticates(app, client, db):
    for junk in ("", "not-a-signed-cookie", "eyJ1c2VyX2lkIjoxfQ.fake.signature"):
        attacker = app.test_client()
        attacker.set_cookie(app.config["SESSION_COOKIE_NAME"], junk)
        assert attacker.get("/orders").status_code == 401


# --- 5. RBAC still works ------------------------------------------------------


def test_5_rbac_is_unaffected_by_the_change(client, db):
    make_and_login(client, "rbac_cust", Role.CUSTOMER)
    assert client.get("/staff/orders").status_code == 403
    assert client.get("/admin/menu").status_code == 403
    client.post("/logout", data={})

    make_and_login(client, "rbac_staff", Role.STAFF)
    assert client.get("/staff/orders").status_code == 200
    assert client.get("/admin/categories").status_code == 403
    client.post("/logout", data={})

    make_and_login(client, "rbac_admin", Role.ADMIN)
    assert client.get("/staff/orders").status_code == 200
    assert client.get("/admin/categories").status_code == 200


def test_5b_revoked_session_cannot_be_used_for_a_privileged_route(app, client, db):
    """A revoked staff session must lose staff access, not merely /orders."""
    make_and_login(client, "revoked_staff", Role.STAFF)
    captured = capture_session(app, client)
    assert replay(app, captured, "/staff/orders") == 200  # control

    client.post("/logout", data={})
    assert replay(app, captured, "/staff/orders") == 401


def test_5c_forged_role_headers_are_still_ignored(app, client, db):
    make_and_login(client, "forger_m09", Role.CUSTOMER)
    forged = {"X-Role": "admin", "X-User-Role": "admin", "X-User-Id": "1"}
    assert client.get("/staff/orders", headers=forged).status_code == 403
    assert client.get("/admin/menu", headers=forged).status_code == 403


# --- 6. CSRF ------------------------------------------------------------------


def test_6_csrf_still_protects_state_changing_routes(app, client, db):
    make_and_login(client, "csrf_m09")
    app.config["WTF_CSRF_ENABLED"] = True

    assert client.post("/preferences", data={"cuisine_preference": "indian"}).status_code == 400
    assert client.post("/preferences",
                       data={"csrf_token": "forged", "cuisine_preference": "indian"}).status_code == 400
    assert client.post("/cart/add", data={"menu_item_id": 1, "quantity": 1}).status_code == 400


# --- 7. No session material in logs -------------------------------------------


def test_7_session_material_never_reaches_the_logs(app, client, db, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    user = make_and_login(client, "logsafe")
    captured = capture_session(app, client)
    client.get("/orders")
    client.post("/logout", data={})
    replay(app, captured)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert captured not in logged, "the session cookie value was written to a log"
    assert "qj_session" not in logged
    assert PASSWORD not in logged
    # The counter is not secret, but there is no reason to log it either.
    assert "session_version" not in logged


def test_7b_session_material_never_reaches_a_response_body(app, client, db):
    make_and_login(client, "bodysafe")
    captured = capture_session(app, client)
    for path in ("/account/", "/account/me", "/orders", "/preferences"):
        body = client.get(path).get_data(as_text=True)
        assert captured not in body
        assert "session_version" not in body
        assert "password_hash" not in body


# --- 8. No authentication regression ------------------------------------------


def test_8_login_logout_login_cycle_is_stable(app, client, db):
    user = make_and_login(client, "cycle")
    for expected_version in (1, 2, 3):
        client.post("/logout", data={})
        assert client.get("/orders").status_code == 401
        _db.session.refresh(user)
        assert user.session_version == expected_version

        login(client, "cycle")
        assert client.get("/orders").status_code == 200


def test_8b_wrong_password_still_rejected_and_does_not_revoke(app, client, db):
    user = make_and_login(client, "wrongpw")
    before = user.session_version

    other = app.test_client()
    forget_cached_user()
    other.post("/login", data={"username": "wrongpw", "password": "not-the-password"})
    forget_cached_user()
    assert other.get("/orders").status_code == 401
    forget_cached_user()

    # A failed login must not disturb the legitimate session.
    _db.session.refresh(user)
    assert user.session_version == before
    assert client.get("/orders").status_code == 200


def test_8c_session_fixation_protection_is_retained(app, client, db):
    """login_user() still clears any pre-authentication session first."""
    client.get("/login")
    before = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    before_value = before.value if before else None

    make_and_login(client, "fixation_m09")
    after = capture_session(app, client)
    assert after != before_value


def test_8d_deactivating_a_user_still_takes_effect_immediately(app, client, db):
    user = make_and_login(client, "deactivated_m09")
    assert client.get("/orders").status_code == 200

    user.is_active = False
    _db.session.commit()
    forget_cached_user()
    assert client.get("/orders").status_code == 401


def test_8e_one_users_logout_does_not_affect_another_user(app, client, db):
    """Revocation is scoped to the account, not global."""
    make_user(username="alice_m09", email="alice_m09@example.com", password=PASSWORD)
    make_user(username="bob_m09", email="bob_m09@example.com", password=PASSWORD)

    bob_client = app.test_client()
    login(bob_client, "bob_m09")
    login(client, "alice_m09")

    forget_cached_user()
    assert client.get("/orders").status_code == 200
    forget_cached_user()
    assert bob_client.get("/orders").status_code == 200

    forget_cached_user()
    client.post("/logout", data={})

    forget_cached_user()
    assert client.get("/orders").status_code == 401
    # Bob is untouched -- revocation is scoped to one account.
    forget_cached_user()
    assert bob_client.get("/orders").status_code == 200


# --- Documented scope: logout revokes *all* of that user's sessions ------------


def test_9_logout_revokes_every_session_for_that_user(app, client, db):
    """Deliberate, documented behaviour (docs/SECURITY.md §6): a single
    counter cannot distinguish sessions, so logging out on one device logs
    the account out everywhere. Pinned here so the choice is explicit rather
    than incidental."""
    make_and_login(client, "multidevice")
    phone = capture_session(app, client)

    laptop = app.test_client()
    login(laptop, "multidevice")
    laptop_cookie = capture_session(app, laptop)

    # Both devices are genuinely authenticated.
    assert replay(app, phone) == 200
    assert replay(app, laptop_cookie) == 200

    forget_cached_user()
    laptop.post("/logout", data={})

    # The laptop's own session ends, and so does the phone's.
    assert replay(app, laptop_cookie) == 401
    assert replay(app, phone) == 401, "logout is documented as revoking all sessions"


def test_10_checkout_flow_still_works_end_to_end(app, client, db):
    """A broad smoke test: session changes must not disturb ordering."""
    item = make_menu_item(category=make_category(name="Mains"), name="Paneer Tikka", price="249.00")
    make_and_login(client, "flow_m09")

    client.post("/cart/add", data={"menu_item_id": item.id, "quantity": 2})
    assert "498.00" in client.get("/cart").get_data(as_text=True)

    resp = client.post("/checkout", data={}, follow_redirects=False)
    assert resp.status_code == 302
    body = client.get("/orders").get_data(as_text=True)
    assert "498.00" in body
