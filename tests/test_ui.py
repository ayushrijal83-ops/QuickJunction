"""Milestone 08 tests: the Bootstrap UI layer.

These cover the finalization gaps M08 introduced or closed -- a homepage that
previously 404'd, a shared layout every page must use, and the template-level
security properties (no |safe, CSRF token on every state-changing form,
escaped output) that the redesign could plausibly have broken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models.user import Role
from tests.conftest import make_category, make_menu_item, make_user

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "app" / "templates"
PASSWORD = "correct-horse-1"


def login(client, username="uicust", role=Role.CUSTOMER):
    user = make_user(username=username, email=f"{username}@example.com",
                     password=PASSWORD, role=role)
    client.post("/login", data={"username": username, "password": PASSWORD})
    return user


# --- Homepage (new in M08) ----------------------------------------------------


def test_homepage_exists_and_is_public(client, db):
    """`/` returned 404 before M08 -- the site had no landing page."""
    make_menu_item(category=make_category(name="Mains"), name="Paneer Tikka", price="249.00")
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Quick Junction" in body
    assert "Paneer Tikka" in body  # available item previewed


def test_homepage_hides_unavailable_and_inactive_items(client, db):
    category = make_category(name="Mains")
    make_menu_item(category=category, name="Available Dish", price="100.00")
    make_menu_item(category=category, name="Sold Out Dish", price="100.00", is_available=False)
    hidden = make_category(name="Retired", is_active=False)
    make_menu_item(category=hidden, name="Hidden Dish", price="100.00")

    body = client.get("/").get_data(as_text=True)
    assert "Available Dish" in body
    assert "Sold Out Dish" not in body
    assert "Hidden Dish" not in body


def test_homepage_works_with_an_empty_menu(client, db):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Nothing is available right now" in resp.get_data(as_text=True)


# --- Shared layout ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/menu", "/login", "/register", "/cart"])
def test_public_pages_use_the_shared_layout(client, db, path):
    make_menu_item(category=make_category(name="Mains"), name="Paneer Tikka", price="249.00")
    body = client.get(path).get_data(as_text=True)
    assert '<meta name="viewport"' in body, "missing responsive viewport"
    assert "vendor/bootstrap.min.css" in body
    assert "navbar" in body


@pytest.mark.parametrize("path", ["/account/", "/preferences", "/orders", "/recommendations"])
def test_authenticated_pages_use_the_shared_layout(client, db, path):
    login(client, "layoutcust")
    body = client.get(path).get_data(as_text=True)
    assert '<meta name="viewport"' in body
    assert "vendor/bootstrap.min.css" in body


def test_bootstrap_is_served_locally_not_from_a_cdn(client, db):
    """The project is offline-first: no page may depend on a CDN."""
    for path in ("/", "/menu", "/login"):
        body = client.get(path).get_data(as_text=True)
        for cdn in ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com", "//maxcdn"):
            assert cdn not in body, f"{path} references {cdn}"

    assert client.get("/static/vendor/bootstrap.min.css").status_code == 200
    assert client.get("/static/vendor/bootstrap.bundle.min.js").status_code == 200


def test_navigation_reflects_role_without_granting_access(client, db):
    """Role-specific links are a display convenience; the routes enforce the
    role regardless of what the navbar shows."""
    login(client, "navcust", Role.CUSTOMER)
    body = client.get("/account/").get_data(as_text=True)
    assert "/staff/orders" not in body
    assert "/admin/menu" not in body
    # ...and the routes deny it anyway.
    assert client.get("/staff/orders").status_code == 403
    assert client.get("/admin/menu").status_code == 403


def test_staff_navigation_shows_the_queue(client, db):
    login(client, "navstaff", Role.STAFF)
    body = client.get("/account/").get_data(as_text=True)
    assert "/staff/orders" in body
    assert client.get("/staff/orders").status_code == 200


# --- Template-level security --------------------------------------------------


def _template_code(path: Path) -> str:
    """Template source with Jinja comments removed, so prose *about* escaping
    is not mistaken for a use of it."""
    return re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)


def test_no_template_uses_safe_or_disables_autoescaping():
    offenders = []
    for path in TEMPLATE_DIR.rglob("*.html"):
        code = _template_code(path)
        if "|safe" in code or "| safe" in code or "autoescape false" in code:
            offenders.append(path.name)
    assert not offenders, f"templates bypassing autoescaping: {offenders}"


def test_the_safe_check_is_not_vacuous(tmp_path):
    """Guard the guard: a template that really did use |safe must be caught."""
    hostile = tmp_path / "hostile.html"
    hostile.write_text("{# a comment mentioning |safe #}\n<p>{{ value|safe }}</p>", encoding="utf-8")
    assert "|safe" in _template_code(hostile)

    innocent = tmp_path / "innocent.html"
    innocent.write_text("{# this template never uses |safe #}\n<p>{{ value }}</p>", encoding="utf-8")
    assert "|safe" not in _template_code(innocent)


def test_every_state_changing_form_carries_a_csrf_token():
    """A redesign is an easy way to accidentally drop a csrf_token field."""
    offenders = []
    for path in TEMPLATE_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if 'method="post"' not in text:
            continue
        if not any(token in text for token in ("csrf_token", "hidden_tag()")):
            offenders.append(path.name)
    assert not offenders, f"POST forms without a CSRF token: {offenders}"


def test_hostile_menu_name_is_escaped_everywhere_it_renders(client, db):
    """Menu names are staff-authored and reach several pages after the
    redesign; every one must escape them."""
    payload = "<script>alert(1)</script>"
    category = make_category(name="Mains")
    item = make_menu_item(category=category, name=payload, price="199.00")

    login(client, "xsscust")
    for path in ("/", "/menu", f"/menu/{item.id}", "/recommendations"):
        body = client.get(path).get_data(as_text=True)
        assert payload not in body, f"unescaped payload on {path}"
        if payload[:8] in body or "script" in body.lower():
            assert "&lt;script&gt;" in body, f"payload not escaped on {path}"


def test_error_pages_do_not_leak_internals(client, db, monkeypatch):
    import app.routes.main as main_routes

    def _boom():
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr(main_routes, "list_public_menu_items", _boom)
    resp = client.get("/")
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "internal detail that must not leak" not in body
    assert "Traceback" not in body
    assert "RuntimeError" not in body


def test_no_secret_reaches_a_rendered_page(app, client, db):
    login(client, "secretcust")
    secret = app.config["SECRET_KEY"]
    for path in ("/", "/account/", "/menu", "/cart", "/orders"):
        body = client.get(path).get_data(as_text=True)
        assert secret not in body
        assert "password_hash" not in body
        assert app.config["SQLALCHEMY_DATABASE_URI"] not in body


# --- Status badges ------------------------------------------------------------


def test_order_status_is_rendered_as_a_badge(client, db):
    from app.models.order import OrderStatus
    from app.services.orders import checkout
    from app.extensions import db as _db

    item = make_menu_item(category=make_category(name="Mains"), name="Paneer Tikka", price="249.00")
    user = login(client, "badgecust")
    order = checkout(user.id, {str(item.id): 1})
    order.status = OrderStatus.PREPARING
    _db.session.commit()

    body = client.get("/orders").get_data(as_text=True)
    assert "badge" in body
    assert "preparing" in body

    detail = client.get(f"/orders/{order.id}").get_data(as_text=True)
    assert "preparing" in detail


# --- Session behaviour (M08 audit finding, fixed in M09) -------------------------------


def test_logout_clears_the_browsers_own_session(client, db):
    login(client, "logoutcust")
    assert client.get("/orders").status_code == 200
    client.post("/logout", data={})
    assert client.get("/orders").status_code == 401


def test_session_revocation_state_exists():
    """Supersedes the M08 guard, which asserted that *no* revocation existed
    and was deliberately written to fail the moment one was added. M09 added
    it, so the assertion is inverted: `users.session_version` is the
    server-side state that makes logout actually revoke. Behavioural coverage
    lives in tests/test_session_revocation.py."""
    from app.models.user import User
    from app.utils import authorization

    assert "session_version" in {c.name for c in User.__table__.columns}

    source = Path(authorization.__file__).read_text(encoding="utf-8")
    assert "session.clear()" in source, "logout must still clear the client session"
    assert "session_version" in source, "logout must consult server-side revocation state"
