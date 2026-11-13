"""O3.1: the CURRENT authorization behaviour of every route, pinned.

Before O3 replaces global-role checks with permissions, this records what
each route does today for each kind of account, so a later milestone that
widens or drops a gate by accident fails here. It records behaviour; it does
not prescribe the future O3 matrix.

How it works
------------
- ``ROUTES`` lists every (URL rule, method) the application registers --
  ``test_route_inventory_is_complete`` fails if a route is added, removed or
  changes method without this table being updated.
- Each row names the route's current gate and a request with *valid* data,
  so an allowed request reaches its normal success response (a validation
  failure would show up as the wrong status, not as a pass).
- ``CLASSES`` (tests/authz_baseline_world.py) are the eight account
  conditions. A session is created exactly as ``login_user`` creates one
  (user id + session version), so these tests exercise the per-request gate:
  ``get_current_user`` plus the route decorators. Login itself is pinned in
  tests/test_authz_baseline_services.py.
- Every (route, class) pair runs against its own fresh database.

Requests are sent as a browser would (``Accept: text/html``): a denial is a
302 to the login page carrying ``next`` (unauthenticated) or an HTML 403
(wrong role). ``test_api_clients_get_json_401`` pins the JSON form.

CSRF is disabled in TestingConfig (``WTF_CSRF_ENABLED = False``), so a CSRF
failure can never be mistaken for an authorization result here; CSRF itself
is covered by the existing security tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from flask import template_rendered

from app.extensions import db
from app.models.customer_preference import CustomerPreference
from app.models.menu_item_ingredient import MenuItemIngredient
from app.models.order import Order, OrderStatus
from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
from app.models.reservation import Reservation, ReservationStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import Role, User
from app.utils.clock import db_today
from tests.authz_baseline_world import CLASSES, PASSWORD, build_world, sign_in

HTML = {"Accept": "text/html"}


@dataclass(frozen=True)
class Route:
    rule: str
    method: str
    gate: str                                   # see GATES
    path: Callable = None                       # world -> URL (default: the rule itself)
    data: Callable = None                       # world -> form data
    ok_status: int = 200
    ok_location: str | None = None              # path prefix of the redirect on success
    ok_text: str | None = None                  # text the success page must contain
    prepare: Callable = None                    # (client, world) -> None, before signing in
    note: str = ""
    # world -> bool: the route's state change, read back from the database.
    # Needed where a refused request redirects exactly like a successful one
    # (see EFFECT_NOTE); must be True when allowed and False when denied.
    effect: Callable = None

    @property
    def id(self) -> str:
        return f"{self.method} {self.rule}"


# What each gate does for each *effective* class (anonymous / customer /
# staff / admin -- see CLASSES). "ok" = the route's own success response.
GATES = {
    # No gate.
    "public":      {"anonymous": "ok", "customer": "ok", "staff": "ok", "admin": "ok"},
    # No gate, but a signed-in account is sent to its dashboard first.
    "signed_out":  {"anonymous": "ok", "customer": "to_account", "staff": "to_account", "admin": "to_account"},
    # @login_required.
    "login":       {"anonymous": "login", "customer": "ok", "staff": "ok", "admin": "ok"},
    # @login_required + ownership in the query: someone else's object is 404.
    "login_owned": {"anonymous": "login", "customer": "ok", "staff": "not_found", "admin": "not_found"},
    # @customer_required (= require_role(CUSTOMER)).
    "customer":    {"anonymous": "login", "customer": "ok", "staff": "forbidden", "admin": "forbidden"},
    # @require_role(STAFF, ADMIN).
    "staff":       {"anonymous": "login", "customer": "forbidden", "staff": "ok", "admin": "ok"},
    # @require_role(ADMIN).
    "admin":       {"anonymous": "login", "customer": "forbidden", "staff": "forbidden", "admin": "ok"},
}


def _cart(client, w):
    client.post("/cart/add", data={"menu_item_id": w.item.id, "quantity": 1})


def _item_form(w, name):
    return {"category_id": w.category.id, "name": name, "description": "", "price": "150.00",
            "cuisine": "indian", "spice_level": "mild", "dietary_type": "vegetarian", "is_available": "y",
            "sort_order": "0", "name_ne": "", "description_ne": "", "ingredients": ""}


def _tomorrow():
    return (db_today() + timedelta(days=1)).isoformat()


EFFECT_NOTE = """These routes redirect to the same place whether the
operation succeeded or a business rule refused it, so status and Location
alone cannot prove success -- nor can they catch a check later moved into
the service (O3.2) that refuses an allowed role. Their ``effect`` reads the
change back from the database."""


def _fresh(model, row_id):
    db.session.expire_all()
    return db.session.get(model, row_id)


def _count(model, **filters):
    db.session.expire_all()
    return db.session.query(model).filter_by(**filters).count()


NEW_PASSWORD = "Changed-pass-77"

ROUTES = [
    # --- public -----------------------------------------------------------------------------
    Route("/", "GET", "public"),
    Route("/health", "GET", "public"),
    Route("/static/<path:filename>", "GET", "public", path=lambda w: "/static/css/app.css"),
    Route("/menu", "GET", "public"),
    Route("/menu/<int:item_id>", "GET", "public", path=lambda w: f"/menu/{w.item.id}", ok_text="Dal Bhat"),
    Route("/locale", "POST", "public", data=lambda w: {"lang": "ne", "next": "/menu"}, ok_status=303,
          ok_location="/menu"),
    Route("/cart", "GET", "public"),
    Route("/cart/add", "POST", "public", data=lambda w: {"menu_item_id": w.item.id, "quantity": 1},
          ok_status=302, ok_location="/cart"),
    Route("/cart/update", "POST", "public", data=lambda w: {"menu_item_id": w.item.id, "quantity": 2},
          ok_status=302, ok_location="/cart", prepare=_cart),
    Route("/cart/remove", "POST", "public", data=lambda w: {"menu_item_id": w.item.id},
          ok_status=302, ok_location="/cart", prepare=_cart),
    Route("/cart/clear", "POST", "public", data=lambda w: {}, ok_status=302, ok_location="/cart", prepare=_cart),

    # --- signed-out only (no gate; signed-in accounts are redirected) --------------------------
    Route("/register", "GET", "signed_out"),
    Route("/register", "POST", "signed_out",
          data=lambda w: {"username": "freshdiner", "email": "freshdiner@example.com", "password": PASSWORD,
                          "password_confirm": PASSWORD},
          ok_status=302, ok_location="/preferences"),
    Route("/login", "GET", "signed_out"),
    Route("/login", "POST", "signed_out", data=lambda w: {"username": "customer", "password": PASSWORD},
          ok_status=302, ok_location="/account/", note="signs in the customer"),
    Route("/login/<any(staff, admin):portal>", "GET", "signed_out", path=lambda w: "/login/staff"),
    Route("/login/<any(staff, admin):portal>", "POST", "signed_out", path=lambda w: "/login/staff",
          data=lambda w: {"username": "staff", "password": PASSWORD}, ok_status=302, ok_location="/account/",
          note="signs in the staff account"),
    Route("/account/setup", "GET", "signed_out"),
    Route("/account/setup", "POST", "signed_out",
          data=lambda w: {"code": "not-a-real-code", "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
          ok_text="invalid or has expired", note="a wrong code re-renders the form: the code is the credential"),

    # --- any signed-in account -------------------------------------------------------------------
    Route("/account/", "GET", "login"),
    Route("/account/password", "GET", "login"),
    Route("/account/password", "POST", "login",
          data=lambda w: {"current_password": PASSWORD, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
          ok_status=302, ok_location="/account/"),
    Route("/logout", "POST", "login", data=lambda w: {}, ok_status=302, ok_location="/login"),
    Route("/orders", "GET", "login", note="lists only the caller's own orders"),
    Route("/orders/<int:order_id>", "GET", "login_owned", path=lambda w: f"/orders/{w.order.id}"),
    Route("/orders/<int:order_id>/cancel", "POST", "login_owned", path=lambda w: f"/orders/{w.order.id}/cancel",
          data=lambda w: {"reason": "changed my mind"}, ok_status=302, ok_location="/orders/",
          effect=lambda w: _fresh(Order, w.order.id).status == OrderStatus.CANCELLED),

    # --- customers only ---------------------------------------------------------------------------
    Route("/checkout", "GET", "customer", prepare=_cart),
    Route("/checkout", "POST", "customer", data=lambda w: {}, prepare=_cart, ok_status=302,
          ok_location="/orders/", effect=lambda w: _count(Order, user_id=w.accounts["customer"].id) == 3),
    Route("/preferences", "GET", "customer"),
    Route("/preferences", "POST", "customer",
          data=lambda w: {"dietary_preference": "vegetarian", "cuisine_preference": "indian",
                          "spice_preference": "mild"},
          ok_status=302, ok_location="/recommendations",
          effect=lambda w: _count(CustomerPreference, user_id=w.accounts["customer"].id) == 1),
    Route("/recommendations", "GET", "customer"),
    Route("/recommendations/explain", "GET", "customer", note="the local model is disabled in tests"),
    Route("/reservations/new", "GET", "customer"),
    Route("/reservations", "POST", "customer",
          data=lambda w: {"table_id": w.table.id, "reservation_date": _tomorrow(), "reservation_time": "13:00",
                          "guest_count": 2},
          ok_status=302, ok_location="/reservations/",
          effect=lambda w: _count(Reservation, user_id=w.accounts["customer"].id) == 2),
    Route("/reservations", "GET", "customer"),
    Route("/reservations/<int:reservation_id>", "GET", "customer",
          path=lambda w: f"/reservations/{w.reservation.id}"),
    Route("/reservations/<int:reservation_id>/cancel", "POST", "customer",
          path=lambda w: f"/reservations/{w.reservation.id}/cancel", data=lambda w: {}, ok_status=302,
          ok_location="/reservations/",
          effect=lambda w: _fresh(Reservation, w.reservation.id).status == ReservationStatus.CANCELLED),

    # --- staff and admin ----------------------------------------------------------------------------
    Route("/staff/orders", "GET", "staff"),
    Route("/staff/orders/new", "GET", "staff"),
    Route("/staff/orders/new", "POST", "staff",
          data=lambda w: {"source": "takeaway", "table_id": "", f"qty_{w.item.id}": "1", "action": "create"},
          ok_status=302, ok_location="/staff/orders/"),
    Route("/staff/orders/<int:order_id>", "GET", "staff", path=lambda w: f"/staff/orders/{w.order.id}",
          note="any order: staff lookups are not ownership-scoped"),
    Route("/staff/orders/<int:order_id>/discount", "POST", "staff",
          path=lambda w: f"/staff/orders/{w.order.id}/discount",
          data=lambda w: {"discount_type": "percent", "discount_value": "10", "discount_reason": "regular"},
          ok_status=302, ok_location="/staff/orders/",
          effect=lambda w: _fresh(Order, w.order.id).discount_amount > 0),
    Route("/staff/orders/<int:order_id>/status", "POST", "staff",
          path=lambda w: f"/staff/orders/{w.order.id}/status", data=lambda w: {"status": "confirmed"},
          ok_status=302, ok_location="/staff/orders/",
          effect=lambda w: _fresh(Order, w.order.id).status == OrderStatus.CONFIRMED),
    Route("/staff/orders/<int:order_id>/payment", "POST", "staff",
          path=lambda w: f"/staff/orders/{w.order.id}/payment",
          data=lambda w: {"amount": str(w.order.total), "method": "cash"}, ok_status=302,
          ok_location="/staff/orders/",
          effect=lambda w: _fresh(Order, w.order.id).amount_settled == w.order.total),
    Route("/staff/kitchen", "GET", "staff"),
    Route("/staff/tables", "GET", "staff"),
    Route("/staff/tables/<int:table_id>/status", "POST", "staff",
          path=lambda w: f"/staff/tables/{w.table.id}/status", data=lambda w: {"status": "reserved"},
          ok_status=302, ok_location="/staff/tables",
          effect=lambda w: _fresh(RestaurantTable, w.table.id).status == TableStatus.RESERVED),
    Route("/staff/reservations", "GET", "staff"),
    Route("/staff/reservations/<int:reservation_id>/status", "POST", "staff",
          path=lambda w: f"/staff/reservations/{w.reservation.id}/status", data=lambda w: {"status": "completed"},
          ok_status=302, ok_location="/staff/reservations",
          effect=lambda w: _fresh(Reservation, w.reservation.id).status == ReservationStatus.COMPLETED),
    Route("/staff/menu", "GET", "staff"),
    Route("/staff/menu/<int:item_id>/availability", "POST", "staff",
          path=lambda w: f"/staff/menu/{w.item.id}/availability", data=lambda w: {"available": "0"},
          ok_status=302, ok_location="/staff/menu"),
    Route("/staff/inventory", "GET", "staff"),
    Route("/staff/inventory/<int:ingredient_id>", "GET", "staff",
          path=lambda w: f"/staff/inventory/{w.ingredient.id}"),
    Route("/staff/inventory/<int:ingredient_id>/movement", "POST", "staff",
          path=lambda w: f"/staff/inventory/{w.ingredient.id}/movement",
          data=lambda w: {"movement_type": "purchase", "quantity": "5", "note": "delivery"}, ok_status=302,
          ok_location="/staff/inventory/",
          effect=lambda w: _fresh(type(w.ingredient), w.ingredient.id).current_quantity == 55),
    Route("/admin/menu", "GET", "staff", note="the menu list is shared by staff and admin"),

    # --- admin only ------------------------------------------------------------------------------------
    Route("/staff/orders/<int:order_id>/payments/<int:entry_id>/refund", "POST", "admin",
          path=lambda w: f"/staff/orders/{w.paid_order.id}/payments/{w.payment_entry.id}/refund",
          data=lambda w: {"amount": "1.00", "reason": "cold food"}, ok_status=302, ok_location="/staff/orders/",
          effect=lambda w: _count(PaymentLedgerEntry, order_id=w.paid_order.id,
                                  entry_type=LedgerEntryType.REFUND) == 1),
    Route("/admin/menu/create", "GET", "admin"),
    Route("/admin/menu/create", "POST", "admin", data=lambda w: _item_form(w, "Momo"), ok_status=302,
          ok_location="/admin/menu"),
    Route("/admin/menu/<int:item_id>/edit", "GET", "admin", path=lambda w: f"/admin/menu/{w.item.id}/edit"),
    Route("/admin/menu/<int:item_id>/edit", "POST", "admin", path=lambda w: f"/admin/menu/{w.item.id}/edit",
          data=lambda w: _item_form(w, "Dal Bhat Set"), ok_status=302, ok_location="/admin/menu"),
    Route("/admin/menu/<int:item_id>/recipe", "GET", "admin", path=lambda w: f"/admin/menu/{w.item.id}/recipe"),
    Route("/admin/menu/<int:item_id>/recipe", "POST", "admin", path=lambda w: f"/admin/menu/{w.item.id}/recipe",
          data=lambda w: {"add_ingredient_id": str(w.ingredient.id), "add_quantity": "0.2"}, ok_status=302,
          ok_location="/admin/menu/",
          effect=lambda w: _count(MenuItemIngredient, menu_item_id=w.item.id, ingredient_id=w.ingredient.id) == 1),
    Route("/admin/categories", "GET", "admin"),
    Route("/admin/categories/create", "GET", "admin"),
    Route("/admin/categories/create", "POST", "admin",
          data=lambda w: {"name": "Desserts", "description": "", "is_active": "y", "sort_order": "1",
                          "name_ne": "", "description_ne": ""},
          ok_status=302, ok_location="/admin/categories"),
    Route("/admin/categories/<int:category_id>/edit", "GET", "admin",
          path=lambda w: f"/admin/categories/{w.category.id}/edit"),
    Route("/admin/categories/<int:category_id>/edit", "POST", "admin",
          path=lambda w: f"/admin/categories/{w.category.id}/edit",
          data=lambda w: {"name": "Main Courses", "description": "", "is_active": "y", "sort_order": "0",
                          "name_ne": "", "description_ne": ""},
          ok_status=302, ok_location="/admin/categories"),
    Route("/admin/inventory/new", "GET", "admin"),
    Route("/admin/inventory/new", "POST", "admin",
          data=lambda w: {"name": "Cheese", "unit": "piece", "minimum_quantity": "4", "is_active": "y"},
          ok_status=302, ok_location="/staff/inventory"),
    Route("/admin/inventory/<int:ingredient_id>/edit", "GET", "admin",
          path=lambda w: f"/admin/inventory/{w.ingredient.id}/edit"),
    Route("/admin/inventory/<int:ingredient_id>/edit", "POST", "admin",
          path=lambda w: f"/admin/inventory/{w.ingredient.id}/edit",
          data=lambda w: {"name": "Basmati", "unit": "kg", "minimum_quantity": "2", "is_active": "y"},
          ok_status=302, ok_location="/staff/inventory"),
    Route("/admin/tables/new", "GET", "admin"),
    Route("/admin/tables/new", "POST", "admin", data=lambda w: {"name": "T9", "capacity": 6}, ok_status=302,
          ok_location="/staff/tables"),
    Route("/admin/tables/<int:table_id>/edit", "GET", "admin", path=lambda w: f"/admin/tables/{w.table.id}/edit"),
    Route("/admin/tables/<int:table_id>/edit", "POST", "admin", path=lambda w: f"/admin/tables/{w.table.id}/edit",
          data=lambda w: {"name": "T1A", "capacity": 5}, ok_status=302, ok_location="/staff/tables"),
    Route("/admin/settings", "GET", "admin"),
    Route("/admin/settings", "POST", "admin", data=lambda w: {"tax_rate": "13", "staff_max_discount": "25"},
          ok_status=302, ok_location="/admin/settings"),
    Route("/admin/settings/restaurant", "GET", "admin"),
    Route("/admin/settings/restaurant", "POST", "admin",
          data=lambda w: {"name": "Cafe Himalaya", "tagline": "", "address": "", "phone": "", "email": "",
                          "default_locale": "en"},
          ok_status=302, ok_location="/admin/settings/restaurant"),
    Route("/admin/payments", "GET", "admin"),
    Route("/admin/reports", "GET", "admin"),
    Route("/admin/audit", "GET", "admin"),
    Route("/admin/accounts", "GET", "admin"),
    Route("/admin/accounts/invite", "GET", "admin"),
    Route("/admin/accounts/invite", "POST", "admin",
          data=lambda w: {"username": "newcook", "email": "newcook@example.com"}, ok_text="One-time code",
          note="success renders the one-time code page (200), not a redirect"),
    Route("/admin/accounts/<int:user_id>/reset", "POST", "admin",
          path=lambda w: f"/admin/accounts/{w.target_staff.id}/reset", data=lambda w: {},
          ok_text="One-time code", note="success renders the one-time code page (200)"),
    Route("/admin/accounts/<int:user_id>/deactivate", "POST", "admin",
          path=lambda w: f"/admin/accounts/{w.target_staff.id}/deactivate", data=lambda w: {}, ok_status=302,
          ok_location="/admin/accounts", effect=lambda w: not _fresh(User, w.target_staff.id).is_active),
    Route("/admin/accounts/<int:user_id>/activate", "POST", "admin",
          path=lambda w: f"/admin/accounts/{w.target_inactive.id}/activate", data=lambda w: {}, ok_status=302,
          ok_location="/admin/accounts", effect=lambda w: _fresh(User, w.target_inactive.id).is_active),
    Route("/admin/staff", "GET", "admin"),
    Route("/admin/staff/<int:user_id>/approve", "POST", "admin",
          path=lambda w: f"/admin/staff/{w.target_pending.id}/approve", data=lambda w: {}, ok_status=302,
          ok_location="/admin/staff", effect=lambda w: _fresh(User, w.target_pending.id).staff_approved),
    Route("/admin/staff/<int:user_id>/revoke", "POST", "admin",
          path=lambda w: f"/admin/staff/{w.target_staff.id}/revoke", data=lambda w: {}, ok_status=302,
          ok_location="/admin/staff", effect=lambda w: not _fresh(User, w.target_staff.id).staff_approved),
]

PROTECTED_GATES = {"login", "login_owned", "customer", "staff", "admin"}


# --- inventory -----------------------------------------------------------------------------------


def test_route_inventory_is_complete(app):
    """Every (rule, method) the application serves has exactly one baseline
    row, and nothing in the table is stale. Adding, removing or re-methoding
    a route fails here until the baseline is updated on purpose."""
    served = {(rule.rule, method) for rule in app.url_map.iter_rules()
              for method in rule.methods - {"HEAD", "OPTIONS"}}
    served = {pair for pair in served if not pair[0].startswith("/_test/")}  # conftest's test-only probes
    pinned = [(r.rule, r.method) for r in ROUTES]
    assert len(pinned) == len(set(pinned)), "duplicate baseline rows"
    assert set(pinned) == served
    # The 71 application URL rules found in O3 Task 1, plus Flask's own
    # static-file rule. A path served by separate GET and POST rules (e.g.
    # /checkout, /reservations) counts twice, as Flask registers it.
    rules = [r for r in app.url_map.iter_rules() if not r.rule.startswith("/_test/")]
    assert len([r for r in rules if r.endpoint != "static"]) == 71
    assert [r.rule for r in rules if r.endpoint == "static"] == ["/static/<path:filename>"]


# --- the matrix --------------------------------------------------------------------------------------


def _expected(route: Route, cls: str) -> str:
    return GATES[route.gate][CLASSES[cls][3]]


def _assert_outcome(resp, outcome: str, route: Route) -> None:
    status, location = resp.status_code, resp.headers.get("Location", "")
    where = urlparse(location)
    if outcome == "login":
        assert status == 302 and where.path == "/login", (status, location)
        assert "next" in parse_qs(where.query), location  # the gate's redirect, not a route's own
    elif outcome == "forbidden":
        assert status == 403, status
    elif outcome == "not_found":
        assert status == 404, status
    elif outcome == "to_account":
        assert status == 302 and where.path == "/account/", (status, location)
    else:  # "ok": the route's own success response
        assert status == route.ok_status, (status, location, resp.get_data(as_text=True)[:300])
        assert "next" not in parse_qs(where.query), location  # never a gate redirect
        if route.ok_location is not None:
            assert where.path.startswith(route.ok_location), location
        if route.ok_text is not None:
            assert route.ok_text in resp.get_data(as_text=True)


def _request(client, route: Route, w, headers=HTML):
    path = route.path(w) if route.path else route.rule
    if route.method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, data=route.data(w) if route.data else {}, headers=headers)


@pytest.mark.parametrize("cls", list(CLASSES))
@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.id)
def test_route_authorization(app, db, route, cls):
    w = build_world()
    client = app.test_client()
    if route.prepare:
        route.prepare(client, w)
    sign_in(client, w.accounts.get(cls))

    resp = _request(client, route, w)
    outcome = _expected(route, cls)
    _assert_outcome(resp, outcome, route)
    if route.effect is not None:
        # Allowed: the change really happened. Denied: nothing changed.
        assert route.effect(w) is (outcome == "ok"), f"{route.id} effect for {cls} ({outcome})"

    if route.gate in PROTECTED_GATES and CLASSES[cls][3] == "anonymous" and cls != "anonymous":
        # An inactive or pending account's session is not just refused: the
        # gate clears it, so it cannot be reused once the account changes.
        with client.session_transaction() as session:
            assert "user_id" not in session


# --- complements ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", [r for r in ROUTES if r.gate in PROTECTED_GATES], ids=lambda r: r.id)
def test_api_clients_get_json_401(app, db, route):
    """A client that does not prefer HTML gets a JSON 401, not a redirect."""
    w = build_world()
    resp = _request(app.test_client(), route, w, headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.get_json() == {"error": {"status": 401, "message": "Authentication required."}}


def test_api_clients_get_json_403(app, db):
    w = build_world()
    client = app.test_client()
    sign_in(client, w.accounts["staff"])
    resp = client.get("/admin/audit", headers={"Accept": "application/json"})
    assert resp.status_code == 403
    assert resp.get_json() == {"error": {"status": 403, "message": "You do not have access to this resource."}}


@pytest.mark.parametrize("cls, template", [
    ("customer", "dashboard/customer.html"), ("staff", "dashboard/staff.html"),
    ("admin", "dashboard/admin.html"), ("pending_admin", "dashboard/admin.html"),
])
def test_dashboard_is_chosen_by_global_role(app, db, cls, template):
    """/account/ renders one dashboard per users.role; only the admin one
    carries the money figures (app/services/dashboard.py)."""
    w = build_world()
    client = app.test_client()
    sign_in(client, w.accounts[cls])
    rendered = []

    def record(sender, template, context, **extra):
        rendered.append((template.name, context))

    with template_rendered.connected_to(record, app):
        assert client.get("/account/", headers=HTML).status_code == 200
    name, context = rendered[0]
    assert name == template
    assert ("month" in context) is (template == "dashboard/admin.html")  # revenue totals: admin only


def test_staff_sees_any_order_but_customer_routes_are_ownership_scoped(app, db):
    """The workforce path to an order is not ownership-scoped (by design);
    the customer path is. Two different gates on the same object."""
    w = build_world()
    staff = app.test_client()
    sign_in(staff, w.accounts["staff"])
    assert staff.get(f"/staff/orders/{w.order_b.id}", headers=HTML).status_code == 200
    assert staff.get(f"/orders/{w.order_b.id}", headers=HTML).status_code == 404


def test_signed_in_login_post_does_not_switch_accounts(app, db):
    """A signed-in user posting someone else's credentials to /login is sent
    to their own dashboard; the session keeps its account."""
    w = build_world()
    client = app.test_client()
    sign_in(client, w.accounts["staff"])
    resp = client.post("/login", data={"username": "customer", "password": PASSWORD}, headers=HTML)
    assert resp.status_code == 302 and urlparse(resp.headers["Location"]).path == "/account/"
    with client.session_transaction() as session:
        assert session["user_id"] == w.accounts["staff"].id


def test_role_constants_are_the_three_global_roles():
    """The whole matrix above rests on exactly these three values."""
    assert [r.value for r in Role] == ["admin", "staff", "customer"]
