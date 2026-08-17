"""Milestone 02 tests: authentication, authorization, CSRF, sessions,
information disclosure, and the audit trail.
"""

from __future__ import annotations

import json

import pytest

from app.models.audit_log import AuditEvent, AuditLog
from app.models.user import Role, User
from app.utils.ratelimit import login_limiter
from tests.conftest import csrf_token, make_user

VALID_PASSWORD = "correct-horse-1"


def _register_data(**overrides) -> dict:
    data = {
        "username": "alice",
        "email": "alice@example.com",
        "password": VALID_PASSWORD,
        "password_confirm": VALID_PASSWORD,
    }
    data.update(overrides)
    return data


def register(client, **overrides):
    return client.post("/register", data=_register_data(**overrides), follow_redirects=False)


def login(client, username: str, password: str):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    login_limiter._attempts.clear()
    yield
    login_limiter._attempts.clear()


# --- Authentication --------------------------------------------------------


def test_1_registration_succeeds_with_valid_data(client, db):
    response = register(client)
    assert response.status_code == 302
    user = db.session.query(User).filter_by(username="alice").one()
    assert user.email == "alice@example.com"
    assert user.role == Role.CUSTOMER


def test_2_duplicate_username_rejected(client, db):
    register(client)
    response = register(client, email="other@example.com")
    assert response.status_code == 200  # re-rendered form, not a redirect
    assert b"already taken" in response.data
    assert db.session.query(User).filter_by(email="other@example.com").first() is None


def test_3_duplicate_email_rejected(client, db):
    register(client)
    response = register(client, username="someoneelse")
    assert response.status_code == 200
    assert b"already registered" in response.data
    assert db.session.query(User).filter_by(username="someoneelse").first() is None


def test_4_weak_password_rejected(client, db):
    response = register(client, password="short1", password_confirm="short1")
    assert response.status_code == 200
    assert db.session.query(User).filter_by(username="alice").first() is None


def test_5_invalid_email_rejected(client, db):
    response = register(client, email="not-an-email")
    assert response.status_code == 200
    assert db.session.query(User).filter_by(username="alice").first() is None


def test_6_password_hash_is_stored_never_plaintext(client, db):
    register(client)
    user = db.session.query(User).filter_by(username="alice").one()
    assert user.password_hash != VALID_PASSWORD
    assert VALID_PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")


def test_7_login_succeeds_with_correct_credentials(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    response = login(client, "alice", VALID_PASSWORD)
    assert response.status_code == 302
    me = client.get("/account/me")
    assert me.status_code == 200
    assert me.get_json()["username"] == "alice"


def test_8_login_fails_with_wrong_password(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    response = login(client, "alice", "wrong-password-1")
    assert response.status_code == 200
    assert client.get("/account/me").status_code == 401


def test_9_logout_works(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    login(client, "alice", VALID_PASSWORD)
    response = client.post("/logout")
    assert response.status_code == 302
    assert client.get("/account/me").status_code == 401


def test_10_unauthenticated_user_cannot_access_protected_endpoint(client):
    response = client.get("/account/me")
    assert response.status_code == 401


# --- Authorization -----------------------------------------------------------


def test_11_customer_cannot_access_admin_endpoint(client, db):
    make_user(username="cust", password=VALID_PASSWORD, role=Role.CUSTOMER)
    login(client, "cust", VALID_PASSWORD)
    assert client.get("/account/admin").status_code == 403


def test_12_customer_cannot_access_staff_endpoint(client, db):
    make_user(username="cust", password=VALID_PASSWORD, role=Role.CUSTOMER)
    login(client, "cust", VALID_PASSWORD)
    assert client.get("/account/staff").status_code == 403


def test_13_staff_cannot_access_admin_endpoint(client, db):
    make_user(username="stf", password=VALID_PASSWORD, role=Role.STAFF)
    login(client, "stf", VALID_PASSWORD)
    assert client.get("/account/staff").status_code == 200
    assert client.get("/account/admin").status_code == 403


def test_14_user_cannot_assign_themselves_admin_role(client, db):
    response = register(client, role="admin")
    assert response.status_code == 302
    user = db.session.query(User).filter_by(username="alice").one()
    assert user.role == Role.CUSTOMER


def test_15_role_changes_are_server_side_only(client, db):
    make_user(username="cust", password=VALID_PASSWORD, role=Role.CUSTOMER)
    login(client, "cust", VALID_PASSWORD)
    # A client-supplied hint must never substitute for the database role.
    response = client.get("/account/admin", headers={"X-Role": "admin"})
    assert response.status_code == 403


# --- CSRF --------------------------------------------------------------------


def test_16_missing_csrf_token_rejected(app, client, db):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post("/login", data={"username": "alice", "password": "x"})
    assert response.status_code == 400


def test_17_invalid_csrf_token_rejected(app, client, db):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post(
        "/login", data={"username": "alice", "password": "x", "csrf_token": "not-a-real-token"}
    )
    assert response.status_code == 400


def test_18_valid_csrf_token_succeeds(app, client, db):
    app.config["WTF_CSRF_ENABLED"] = True
    token = csrf_token(client, "/login")
    response = client.post("/login", data={"username": "alice", "password": "x", "csrf_token": token})
    assert response.status_code == 200  # reached the view; CSRF check passed


# --- Session -------------------------------------------------------------------


def test_19_session_cookie_security_flags_are_correct(app):
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    # Secure is relaxed only in development/testing (plain-HTTP localhost);
    # production forces it on -- see test_foundation.test_production_disables_debug_and_hardens_session.
    assert app.config["ENV_NAME"] in {"development", "testing"}
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_20_logout_invalidates_authentication(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    login(client, "alice", VALID_PASSWORD)
    assert client.get("/account/me").status_code == 200
    client.post("/logout")
    assert client.get("/account/me").status_code == 401
    assert client.get("/account/admin").status_code == 401  # not 403: no longer authenticated at all


# --- Information disclosure -----------------------------------------------------


def test_21_login_errors_do_not_distinguish_unknown_user_from_wrong_password(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    unknown_user = login(client, "nobody-registered", "whatever12")
    wrong_password = login(client, "alice", "wrong-password-1")
    assert unknown_user.status_code == wrong_password.status_code == 200
    assert b"Invalid username or password" in unknown_user.data
    assert b"Invalid username or password" in wrong_password.data


def test_22_password_hash_never_appears_in_responses(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    login(client, "alice", VALID_PASSWORD)
    response = client.get("/account/me")
    body = response.get_data(as_text=True)
    assert "password_hash" not in body
    assert "$argon2id$" not in body


def test_23_exceptions_do_not_expose_stack_traces(app, client, db):
    @app.get("/_boom_auth")
    def boom():
        raise RuntimeError("should never reach the client")

    response = client.get("/_boom_auth")
    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "RuntimeError" not in body
    assert response.get_json()["error"]["message"] == "An internal error occurred."


def test_24_database_errors_are_not_exposed_to_users(client, db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def _boom(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(db.session, "commit", _boom)
    response = register(client)
    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert "OperationalError" not in body
    assert "connection refused" not in body


# --- Audit -----------------------------------------------------------------------


def test_25_successful_login_is_recorded(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    login(client, "alice", VALID_PASSWORD)
    entry = db.session.query(AuditLog).filter_by(event_type=AuditEvent.LOGIN_SUCCESS).one()
    assert entry.success is True
    assert entry.user_id is not None


def test_26_failed_login_is_recorded(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    login(client, "alice", "wrong-password-1")
    entry = db.session.query(AuditLog).filter_by(event_type=AuditEvent.LOGIN_FAILURE).one()
    assert entry.success is False


def test_27_logout_is_recorded(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    login(client, "alice", VALID_PASSWORD)
    client.post("/logout")
    entry = db.session.query(AuditLog).filter_by(event_type=AuditEvent.LOGOUT).one()
    assert entry.success is True


def test_28_sensitive_values_are_absent_from_audit_logs(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    login(client, "alice", "wrong-password-1")
    login(client, "alice", VALID_PASSWORD)
    entries = db.session.query(AuditLog).all()
    assert entries, "expected at least one audit row"
    for entry in entries:
        blob = json.dumps(
            {
                "event_type": entry.event_type.value,
                "metadata": entry.metadata_json,
                "user_agent": entry.user_agent,
                "ip_address": entry.ip_address,
            }
        )
        assert VALID_PASSWORD not in blob
        assert "wrong-password-1" not in blob
        assert "argon2" not in blob


# --- Rate limiting (non-trivial logic; ponytail's one-check rule) --------------


def test_login_rate_limiting_blocks_after_repeated_failures(client, db):
    make_user(username="alice", password=VALID_PASSWORD)
    for _ in range(5):
        login(client, "alice", "wrong-password-1")
    limited = login(client, "alice", VALID_PASSWORD)  # even the right password is blocked
    assert limited.status_code == 429
    assert client.get("/account/me").status_code == 401
