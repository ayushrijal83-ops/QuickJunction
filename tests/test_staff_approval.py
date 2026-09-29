"""Phase 3: separate customer / staff / admin sign-in, admin approval of
staff accounts, and customer-only preferences and suggestions.

Server-side enforcement is what is under test here: every check goes through
the real routes, and nothing relies on what a template shows or hides.
"""

from __future__ import annotations

import pytest
from flask import g

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.user import Role, User
from app.utils.ratelimit import login_limiter
from tests.conftest import csrf_token, make_user

PASSWORD = "correct-horse-1"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    login_limiter._attempts.clear()
    yield
    login_limiter._attempts.clear()


def fresh():
    """The test app context outlives requests, so g.current_user would be
    reused across them. A real server re-reads the user on every request;
    clearing g is what makes the next request here do the same."""
    g.pop("current_user", None)


def login(client, username, portal=""):
    fresh()
    path = f"/login/{portal}" if portal else "/login"
    return client.post(path, data={"username": username, "password": PASSWORD})


def logout(client):
    client.post("/logout", data={})
    fresh()


def get(client, path):
    fresh()
    return client.get(path)


def post(client, path, **data):
    fresh()
    return client.post(path, data=data)


def register_staff(client, username="newcook", **extra):
    fresh()
    return client.post("/register/staff", data={
        "username": username, "email": f"{username}@example.com",
        "password": PASSWORD, "password_confirm": PASSWORD, **extra,
    })


def user_named(username) -> User:
    _db.session.expire_all()
    return _db.session.query(User).filter_by(username=username).one()


# --- 1-3. New staff start pending and get nothing -----------------------------


def test_1_new_staff_account_defaults_to_pending(client, db):
    response = register_staff(client)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login/staff")

    user = user_named("newcook")
    assert user.role == Role.STAFF
    assert user.staff_approved is False
    # Not signed in by registration, unlike a customer.
    assert get(client, "/account/me").status_code == 401
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.STAFF_REGISTERED).count() == 1


def test_2_pending_staff_cannot_sign_in_or_reach_the_dashboard(client, db):
    register_staff(client)
    response = login(client, "newcook", "staff")
    assert response.status_code == 200
    assert "waiting for administrator approval" in response.get_data(as_text=True)
    assert get(client, "/account/").status_code in (302, 401)
    assert get(client, "/staff/orders").status_code == 401


def test_2b_pending_message_needs_the_right_password(client, db):
    register_staff(client)
    fresh()
    response = client.post("/login/staff", data={"username": "newcook", "password": "wrong-password-1"})
    body = response.get_data(as_text=True)
    assert "Invalid username or password." in body
    assert "approval" not in body


def test_3_pending_staff_cannot_use_staff_order_routes(client, db):
    make_user(username="pending", email="pending@example.com", password=PASSWORD,
              role=Role.STAFF, staff_approved=False)
    login(client, "pending", "staff")
    assert get(client, "/staff/orders").status_code == 401
    assert get(client, "/staff/orders/1").status_code == 401
    assert post(client, "/staff/orders/1/status", status="confirmed").status_code == 401


# --- 4-6. Only admins approve -------------------------------------------------


def test_4_customer_cannot_access_staff_or_admin_routes(client, db):
    make_user(username="cust", email="cust@example.com", password=PASSWORD)
    login(client, "cust")
    for path in ("/staff/orders", "/admin/staff", "/admin/menu"):
        assert get(client, path).status_code == 403, path


def test_5_customer_cannot_approve_staff(client, db):
    pending = make_user(username="pending", email="pending@example.com", password=PASSWORD,
                        role=Role.STAFF, staff_approved=False)
    make_user(username="cust", email="cust@example.com", password=PASSWORD)
    login(client, "cust")
    assert post(client, f"/admin/staff/{pending.id}/approve").status_code == 403
    assert user_named("pending").staff_approved is False


def test_6_staff_cannot_approve_themselves_or_others(client, db):
    approved = make_user(username="cook", email="cook@example.com", password=PASSWORD, role=Role.STAFF)
    other = make_user(username="pending", email="pending@example.com", password=PASSWORD,
                      role=Role.STAFF, staff_approved=False)
    login(client, "cook", "staff")
    assert post(client, f"/admin/staff/{other.id}/approve").status_code == 403
    assert post(client, f"/admin/staff/{approved.id}/revoke").status_code == 403
    assert user_named("pending").staff_approved is False
    assert user_named("cook").staff_approved is True


def test_6b_a_pending_account_cannot_approve_itself(client, db):
    register_staff(client)
    user = user_named("newcook")
    login(client, "newcook", "staff")
    assert post(client, f"/admin/staff/{user.id}/approve").status_code == 401
    assert user_named("newcook").staff_approved is False


# --- 7-9. Approve, use, revoke ------------------------------------------------


def test_7_8_admin_approves_and_staff_can_then_work(client, db):
    make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    register_staff(client)
    user = user_named("newcook")

    login(client, "boss", "admin")
    listing = get(client, "/admin/staff").get_data(as_text=True)
    assert "newcook" in listing and "Pending approval" in listing
    response = post(client, f"/admin/staff/{user.id}/approve")
    assert response.status_code == 302
    assert user_named("newcook").staff_approved is True
    event = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.STAFF_APPROVED).one()
    assert f'"staff_user_id": {user.id}' in event.metadata_json
    logout(client)

    login(client, "newcook", "staff")
    assert get(client, "/staff/orders").status_code == 200
    assert get(client, "/account/me").get_json()["role"] == "staff"


def test_9_revocation_blocks_the_existing_session_on_the_next_request(app, client, db):
    staff = make_user(username="cook", email="cook@example.com", password=PASSWORD, role=Role.STAFF)
    make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    login(client, "cook", "staff")
    assert get(client, "/staff/orders").status_code == 200

    admin_client = app.test_client()
    login(admin_client, "boss", "admin")
    assert post(admin_client, f"/admin/staff/{staff.id}/revoke").status_code == 302
    assert _db.session.query(AuditLog).filter_by(event_type=AuditEvent.STAFF_APPROVAL_REVOKED).count() == 1

    # The staff member's still-held cookie no longer authenticates.
    assert get(client, "/staff/orders").status_code == 401
    assert get(client, "/account/me").status_code == 401
    assert "waiting for administrator approval" in login(client, "cook", "staff").get_data(as_text=True)


def test_9b_approval_endpoints_only_touch_staff_accounts(client, db):
    make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    cust = make_user(username="cust", email="cust@example.com", password=PASSWORD)
    login(client, "boss", "admin")
    assert post(client, f"/admin/staff/{cust.id}/approve").status_code == 404
    assert post(client, "/admin/staff/99999/approve").status_code == 404
    assert user_named("cust").role == Role.CUSTOMER
    # GET cannot change state.
    assert get(client, f"/admin/staff/{cust.id}/approve").status_code == 405


# --- 10. Portals --------------------------------------------------------------


@pytest.mark.parametrize("role, portal", [
    (Role.CUSTOMER, "staff"), (Role.CUSTOMER, "admin"),
    (Role.STAFF, "admin"), (Role.ADMIN, "staff"),
])
def test_10_a_portal_refuses_other_account_types(client, db, role, portal):
    make_user(username="someone", email="someone@example.com", password=PASSWORD, role=role)
    response = login(client, "someone", portal)
    assert response.status_code == 200
    assert f"does not have {portal} access" in response.get_data(as_text=True)
    assert get(client, "/account/me").status_code == 401


@pytest.mark.parametrize("role, portal, allowed, denied", [
    (Role.CUSTOMER, "", "/recommendations", "/staff/orders"),
    (Role.STAFF, "staff", "/staff/orders", "/admin/staff"),
    (Role.ADMIN, "admin", "/admin/staff", None),
])
def test_10b_each_role_signs_in_to_its_own_portal(client, db, role, portal, allowed, denied):
    make_user(username="member", email="member@example.com", password=PASSWORD, role=role)
    response = login(client, "member", portal)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account/")
    assert get(client, allowed).status_code == 200
    if denied:
        assert get(client, denied).status_code == 403


def test_10c_login_pages_render_for_each_portal(client, db):
    for path, heading in (("/login", "Customer sign in"), ("/login/staff", "Staff sign in"),
                          ("/login/admin", "Admin sign in")):
        body = get(client, path).get_data(as_text=True)
        assert heading in body
    assert get(client, "/login/root").status_code == 404


def test_10d_next_cannot_point_at_another_login_page_or_bypass_roles(client, db):
    make_user(username="cust", email="cust@example.com", password=PASSWORD)
    fresh()
    response = client.post("/login?next=/login/admin", data={"username": "cust", "password": PASSWORD})
    assert not response.headers["Location"].endswith("/login/admin")
    # A next pointing at an admin page is followed, and the admin page refuses.
    logout(client)
    fresh()
    response = client.post("/login?next=/admin/staff", data={"username": "cust", "password": PASSWORD})
    assert response.headers["Location"].endswith("/admin/staff")
    assert get(client, "/admin/staff").status_code == 403


# --- 11. CSRF -----------------------------------------------------------------


def test_11_approval_requires_a_csrf_token(app, client, db):
    make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    pending = make_user(username="pending", email="pending@example.com", password=PASSWORD,
                        role=Role.STAFF, staff_approved=False)
    login(client, "boss", "admin")
    app.config["WTF_CSRF_ENABLED"] = True

    assert post(client, f"/admin/staff/{pending.id}/approve").status_code == 400
    assert user_named("pending").staff_approved is False

    token = csrf_token(client, "/admin/staff")
    assert post(client, f"/admin/staff/{pending.id}/approve", csrf_token=token).status_code == 302
    assert user_named("pending").staff_approved is True


# --- 12. No role escalation through registration ------------------------------


def test_12_registration_ignores_role_and_approval_fields(client, db):
    fresh()
    client.post("/register", data={
        "username": "sneaky", "email": "sneaky@example.com", "password": PASSWORD,
        "password_confirm": PASSWORD, "role": "admin", "is_admin": "true", "staff_approved": "1",
    })
    assert user_named("sneaky").role == Role.CUSTOMER
    logout(client)

    register_staff(client, "sneakycook", role="admin", is_admin="true", staff_approved="1")
    user = user_named("sneakycook")
    assert user.role == Role.STAFF
    assert user.staff_approved is False


# --- Customer-only preferences and suggestions -------------------------------


@pytest.mark.parametrize("role", [Role.STAFF, Role.ADMIN])
def test_staff_and_admin_have_no_preferences_or_suggestions(client, db, role):
    make_user(username="member", email="member@example.com", password=PASSWORD, role=role)
    login(client, "member", role.value)
    for path in ("/preferences", "/recommendations", "/recommendations/explain"):
        assert get(client, path).status_code == 403, path
    assert post(client, "/preferences", cuisine_preference="indian").status_code == 403

    for page in ("/account/", "/"):
        body = get(client, page).get_data(as_text=True)
        assert "/recommendations" not in body, page
        assert "/preferences" not in body, page


def test_customer_keeps_preferences_and_suggestions(client, db):
    make_user(username="cust", email="cust@example.com", password=PASSWORD)
    login(client, "cust")
    for path in ("/preferences", "/recommendations", "/recommendations/explain"):
        assert get(client, path).status_code == 200, path
    assert "/recommendations" in get(client, "/account/").get_data(as_text=True)


def test_admin_dashboard_shows_pending_count(client, db):
    make_user(username="boss", email="boss@example.com", password=PASSWORD, role=Role.ADMIN)
    make_user(username="p1", email="p1@example.com", password=PASSWORD, role=Role.STAFF, staff_approved=False)
    make_user(username="p2", email="p2@example.com", password=PASSWORD, role=Role.STAFF, staff_approved=False)
    login(client, "boss", "admin")
    assert "2 pending" in get(client, "/account/").get_data(as_text=True)
