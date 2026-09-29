"""Milestone 16: final security audit checks that sweep the whole app, so a
route or template added later without the right guard fails here."""

from __future__ import annotations

import pytest
from flask import g

from app.models.user import Role
from app.utils.headers import CONTENT_SECURITY_POLICY
from tests.conftest import make_user

PASSWORD = "correct-horse-1"

# Everything an anonymous visitor may use. Anything not listed must refuse.
PUBLIC_ENDPOINTS = {
    "main.home", "health.health", "menu.index", "menu.detail",
    "auth.login", "auth.register", "auth.register_staff",
    "cart.index", "cart.add", "cart.update", "cart.remove", "cart.clear",
}
# Endpoints only STAFF/ADMIN may reach, and those only ADMIN may reach.
STAFF_PREFIXES = ("staff_orders.", "kitchen.", "tables.board", "tables.set_status", "inventory.stock",
                  "inventory.detail", "inventory.movement", "reservations.staff_")
ADMIN_ONLY_PREFIXES = ("admin_settings.", "admin_views.", "admin_staff.", "reports.", "inventory.new",
                       "inventory.edit", "inventory.recipe", "tables.new", "tables.edit")


def _routes(app):
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.endpoint == "static":
            continue
        url = rule.rule
        for arg in rule.arguments:
            url = url.replace(f"<int:{arg}>", "1")
        url = url.replace("<any(staff, admin):portal>", "staff")
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            yield rule.endpoint, method, url


def _login(client, role):
    make_user(username=f"audit_{role.value}", email=f"audit_{role.value}@example.com", password=PASSWORD, role=role)
    g.pop("current_user", None)
    client.post("/login", data={"username": f"audit_{role.value}", "password": PASSWORD})


def test_every_non_public_route_refuses_anonymous_callers(app, client, db):
    assert sum(1 for _ in _routes(app)) >= 78  # the sweep really covers the app
    reachable = []
    for endpoint, method, url in _routes(app):
        if endpoint in PUBLIC_ENDPOINTS:
            continue
        g.pop("current_user", None)
        status = getattr(client, method.lower())(url).status_code
        if status != 401:
            reachable.append((status, method, url, endpoint))
    assert reachable == [], reachable


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.STAFF])
def test_admin_only_routes_refuse_customers_and_staff(app, client, db, role):
    assert sum(e.startswith(ADMIN_ONLY_PREFIXES) for e, _, _ in _routes(app)) >= 18
    _login(client, role)
    leaks = []
    for endpoint, method, url in _routes(app):
        if endpoint.startswith(ADMIN_ONLY_PREFIXES):
            g.pop("current_user", None)
            status = getattr(client, method.lower())(url).status_code
            if status != 403:
                leaks.append((status, method, url))
    assert leaks == [], leaks


def test_staff_routes_refuse_customers(app, client, db):
    assert sum(e.startswith(STAFF_PREFIXES) for e, _, _ in _routes(app)) >= 14
    _login(client, Role.CUSTOMER)
    leaks = []
    for endpoint, method, url in _routes(app):
        if endpoint.startswith(STAFF_PREFIXES):
            g.pop("current_user", None)
            status = getattr(client, method.lower())(url).status_code
            if status != 403:
                leaks.append((status, method, url))
    assert leaks == [], leaks


def test_security_headers_on_every_response(client, db):
    for path in ("/", "/menu", "/login", "/health", "/does-not-exist"):
        headers = client.get(path).headers
        assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY, path
        assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff", path
        assert headers["Referrer-Policy"] == "same-origin", path
    assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY


def test_signed_in_pages_are_not_cached_but_public_ones_may_be(client, db):
    assert "no-store" not in client.get("/menu").headers.get("Cache-Control", "")
    _login(client, Role.CUSTOMER)
    for path in ("/account/", "/orders", "/menu"):
        g.pop("current_user", None)
        assert client.get(path).headers["Cache-Control"] == "no-store", path
    assert "no-store" not in client.get("/static/css/app.css").headers.get("Cache-Control", "")


def test_no_inline_script_left_in_templates():
    """The CSP forbids inline script; an inline handler or <script> block
    added later would silently stop working, so catch it here."""
    import re
    from pathlib import Path

    offenders = []
    for path in Path("app/templates").rglob("*.html"):
        code = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        if re.search(r"\son(click|change|submit|load|input|error|focus|blur|key\w+|mouse\w+)\s*=", code, re.I):
            offenders.append(f"{path}: inline handler")
        for tag in re.findall(r"<script\b[^>]*>", code, re.I):
            if "src=" not in tag:
                offenders.append(f"{path}: inline <script>")
    assert offenders == [], offenders


def test_debug_is_off_outside_development(app):
    from config import ProductionConfig, TestingConfig

    assert TestingConfig.DEBUG is False and ProductionConfig.DEBUG is False
    assert app.debug is False
