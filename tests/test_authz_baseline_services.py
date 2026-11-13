"""O3.1: the CURRENT authorization behaviour below the routes, pinned --
sign-in by account class, customer object ownership, and the sensitive
service functions called directly.

Records behaviour, it does not prescribe it. Several services have no
authorization check of their own and rely entirely on the route in front of
them; those are pinned by tests named ``test_gap_*`` that assert the call
*succeeds* for an actor the route would refuse. They are baseline findings
for later O3 milestones: when a milestone adds the missing check, the
matching ``test_gap_*`` test must be changed on purpose, never deleted
silently.

Every refusal is asserted by its message (``refused``), so a refusal for the
intended reason -- e.g. the role check -- can never be confused with an
unrelated validation failure.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from decimal import Decimal

import pytest
from flask import g

from app.extensions import db as _db
from app.models.ingredient import Ingredient
from app.models.order import Order, OrderStatus
from app.models.organization import OrganizationMembership
from app.models.reservation import Reservation, ReservationStatus
from app.models.user import Role, User
from app.services import inventory, orders, organization, payment_ledger, pricing, reservations, tables
from app.services.accounts import invite_staff, issue_password_reset, set_active
from app.services.errors import ValidationError
from app.services.menu import set_availability
from app.services.orders import checkout, place_staff_order, update_order_status
from app.services.restaurant import RestaurantInput, update_restaurant
from tests.authz_baseline_world import PASSWORD, build_world, sign_in

HTML = {"Accept": "text/html"}
ADMIN_ONLY = "Only an administrator can do that."


@pytest.fixture
def w(db):
    return build_world()


def _reload(model, row_id):
    _db.session.expire_all()
    return _db.session.get(model, row_id)


@contextmanager
def refused(message: str):
    """A ValidationError whose messages contain ``message``."""
    with pytest.raises(ValidationError) as exc:
        yield
    assert message in str(exc.value.errors), exc.value.errors


# ======================================================================================
# Sign-in by account class (the login form, not a forged session)
# ======================================================================================


@pytest.mark.parametrize("account, portal, outcome", [
    ("customer", "", "signed_in"),
    ("staff", "/staff", "signed_in"),
    ("admin", "/admin", "signed_in"),
    ("pending_admin", "/admin", "signed_in"),         # staff_approved means nothing for ADMIN
    ("inactive_staff", "/staff", "Invalid username or password."),
    ("inactive_admin", "/admin", "Invalid username or password."),
    ("pending_staff", "/staff", "waiting for administrator approval"),
    ("staff", "/admin", "does not have admin access"),
    ("admin", "/staff", "does not have staff access"),
    ("customer", "/staff", "does not have staff access"),
])
def test_sign_in_by_account_class(app, w, account, portal, outcome):
    """With the correct password: inactive accounts get the same generic
    failure as a wrong password; pending staff are told to wait; a role that
    does not match the portal is refused. None of these creates a session."""
    client = app.test_client()
    resp = client.post(f"/login{portal}", data={"username": account, "password": PASSWORD}, headers=HTML)
    with client.session_transaction() as session:
        signed_in_as = session.get("user_id")
    if outcome == "signed_in":
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/account/")
        assert signed_in_as == w.accounts[account].id
    else:
        assert resp.status_code == 200 and outcome in resp.get_data(as_text=True)
        assert signed_in_as is None


def test_a_session_dies_when_its_account_is_deactivated(app, w):
    """Deactivation bumps session_version: the existing session stops
    authenticating on its next request."""
    client = app.test_client()
    sign_in(client, w.accounts["staff"])
    assert client.get("/staff/orders", headers=HTML).status_code == 200
    g.pop("current_user", None)
    set_active(w.accounts["admin"], w.accounts["staff"], False)
    g.pop("current_user", None)
    assert client.get("/staff/orders", headers=HTML).status_code == 302


def test_a_session_dies_when_staff_approval_is_revoked(app, w):
    """No session_version bump here: get_current_user re-reads
    staff_approved on every request."""
    client = app.test_client()
    sign_in(client, w.accounts["staff"])
    assert client.get("/staff/orders", headers=HTML).status_code == 200
    w.accounts["staff"].staff_approved = False
    _db.session.commit()
    g.pop("current_user", None)
    assert client.get("/staff/orders", headers=HTML).status_code == 302


# ======================================================================================
# Customer object ownership (two customers, real persisted objects)
# ======================================================================================


def _as(app, user):
    client = app.test_client()
    sign_in(client, user)
    return client


def test_customers_see_only_their_own_orders(app, w):
    a = _as(app, w.accounts["customer"])
    assert a.get(f"/orders/{w.order.id}", headers=HTML).status_code == 200
    assert a.get(f"/orders/{w.order_b.id}", headers=HTML).status_code == 404  # same as a missing id
    assert a.get("/orders/999999", headers=HTML).status_code == 404
    page = a.get("/orders", headers=HTML).get_data(as_text=True)
    assert f"/orders/{w.order.id}" in page and f"/orders/{w.order_b.id}" not in page


def test_customers_cannot_cancel_another_customers_order(app, w):
    a = _as(app, w.accounts["customer"])
    assert a.post(f"/orders/{w.order_b.id}/cancel", data={"reason": "x"}, headers=HTML).status_code == 404
    assert _reload(Order, w.order_b.id).status == OrderStatus.PENDING
    assert a.post(f"/orders/{w.order.id}/cancel", data={"reason": "x"}, headers=HTML).status_code == 302
    assert _reload(Order, w.order.id).status == OrderStatus.CANCELLED


def test_customer_cancellation_is_also_ownership_scoped_in_the_service(w):
    """cancel_order_as_customer re-checks ownership inside its UPDATE, not
    only through the route's lookup."""
    with pytest.raises(ValidationError):
        orders.cancel_order_as_customer(w.order_b, w.accounts["customer"])
    assert _reload(Order, w.order_b.id).status == OrderStatus.PENDING


def test_customers_see_only_their_own_reservations(app, w):
    a = _as(app, w.accounts["customer"])
    assert a.get(f"/reservations/{w.reservation.id}", headers=HTML).status_code == 200
    assert a.get(f"/reservations/{w.reservation_b.id}", headers=HTML).status_code == 404
    page = a.get("/reservations", headers=HTML).get_data(as_text=True)
    assert f"/reservations/{w.reservation.id}" in page and f"/reservations/{w.reservation_b.id}" not in page


def test_customers_cannot_cancel_another_customers_reservation(app, w):
    a = _as(app, w.accounts["customer"])
    assert a.post(f"/reservations/{w.reservation_b.id}/cancel", headers=HTML).status_code == 404
    assert _reload(Reservation, w.reservation_b.id).status == ReservationStatus.CONFIRMED


@pytest.mark.parametrize("worker", ["staff", "admin"])
def test_workforce_roles_do_not_replace_customer_ownership(app, w, worker):
    """Staff and admins reach customer orders through /staff/orders, never
    through the customer routes: there the ownership filter still applies."""
    c = _as(app, w.accounts[worker])
    assert c.get(f"/orders/{w.order.id}", headers=HTML).status_code == 404
    assert c.post(f"/orders/{w.order.id}/cancel", data={"reason": "x"}, headers=HTML).status_code == 404
    assert _reload(Order, w.order.id).status == OrderStatus.PENDING
    assert c.get(f"/reservations/{w.reservation.id}", headers=HTML).status_code == 403  # customer_required


# ======================================================================================
# Payments, refunds, discounts, pricing
# ======================================================================================


def test_gap_record_ledger_payment_has_no_actor_check(w):
    """Baseline finding: the service records a payment for ANY actor -- here
    a customer. Only @require_role(STAFF, ADMIN) on the route stops it."""
    entry = payment_ledger.record_ledger_payment(w.order, str(w.order.total), "cash", w.accounts["customer"])
    assert entry.recorded_by_id == w.accounts["customer"].id


@pytest.mark.parametrize("actor", ["staff", "customer"])
def test_gap_refund_ledger_payment_has_no_actor_check(w, actor):
    """Baseline finding: refunds are ADMIN-only at the route
    (staff_orders.py, @require_role(ADMIN)) but the service accepts any actor."""
    refund = payment_ledger.refund_ledger_payment(w.payment_entry, "1.00", "baseline", w.accounts[actor])
    assert refund.amount == Decimal("-1.00")


def test_discount_service_checks_role_and_cap(w):
    staff, admin, customer = w.accounts["staff"], w.accounts["admin"], w.accounts["customer"]
    with refused("Only staff can apply discounts."):
        pricing.apply_discount(w.order, "percent", "5", "x", customer)
    with refused("Your role may discount at most 20.00 % of the subtotal."):    # the staff cap
        pricing.apply_discount(w.order, "percent", "50", "x", staff)
    assert pricing.apply_discount(w.order, "percent", "20", "x", staff).discount_amount > 0
    assert pricing.apply_discount(w.order, "percent", "100", "x", admin).total == Decimal("0.00")


@pytest.mark.parametrize("actor", ["staff", "customer"])
def test_gap_update_pricing_settings_has_no_actor_check(w, actor):
    """Baseline finding: tax rate and staff discount cap are ADMIN-only at the
    route (/admin/settings) but update_settings accepts any actor."""
    pricing.update_settings("5", "30", w.accounts[actor])
    assert pricing.current_settings() == (Decimal("5.00"), Decimal("30.00"))


def test_restaurant_profile_service_requires_admin(w):
    data = RestaurantInput("Cafe", None, None, None, None, "en")
    with refused("Only an administrator can change the restaurant profile."):
        update_restaurant(w.accounts["staff"], data)
    assert update_restaurant(w.accounts["admin"], data)


# ======================================================================================
# Inventory, menu, orders, tables, reservations
# ======================================================================================


def test_inventory_adjustment_is_admin_only_in_the_service(w):
    """An adjustment's quantity is a signed change, not an absolute count."""
    with refused("Only an admin can adjust stock counts."):
        inventory.record_movement(w.ingredient, "adjustment", "100", "count", w.accounts["staff"])
    inventory.record_movement(w.ingredient, "adjustment", "100", "count", w.accounts["admin"])
    assert _reload(Ingredient, w.ingredient.id).current_quantity == Decimal("150")   # 50 + 100


def test_gap_inventory_purchase_has_no_actor_role_check(w):
    """Baseline finding: only the adjustment type is role-checked; any actor
    (here a customer) can record a purchase/waste/restock movement."""
    inventory.record_movement(w.ingredient, "purchase", "5", None, w.accounts["customer"])
    assert _reload(Ingredient, w.ingredient.id).current_quantity == Decimal("55")


def test_menu_availability_service_requires_staff_or_admin(w):
    with refused("Only staff can change availability."):
        set_availability(w.accounts["customer"], w.item.id, False)
    assert set_availability(w.accounts["staff"], w.item.id, False)


def test_gap_order_status_transition_has_no_actor_role_check(w):
    """Baseline finding: update_order_status validates the transition, not
    the actor -- a customer actor can confirm an order. The route
    (@require_role(STAFF, ADMIN)) is the only gate."""
    update_order_status(w.order, OrderStatus.CONFIRMED, actor=w.accounts["customer"])
    assert _reload(Order, w.order.id).status == OrderStatus.CONFIRMED


def test_order_entry_wrappers_check_role(w):
    """The two entry points each check the actor's role before anything is
    written: checkout_as_customer (orders.py:134) and place_staff_order
    (orders.py:158)."""
    cart = {str(w.item.id): 1}
    with refused("Only staff can enter orders here."):
        place_staff_order(w.accounts["customer"], cart, "takeaway", None)
    with refused("Only customer accounts can place orders here."):
        orders.checkout_as_customer(w.accounts["staff"], cart, "online", None)


def test_gap_core_checkout_has_no_role_check(w):
    """Baseline finding: the shared core both wrappers call, checkout(), takes
    a user id and checks no role -- it is safe only because every route calls
    a wrapper."""
    order = checkout(w.accounts["staff"].id, {str(w.item.id): 1}, "online", None)
    assert order.user_id == w.accounts["staff"].id


@pytest.mark.parametrize("service, parameters", [
    (tables.create_table, ["name", "capacity"]),
    (tables.update_table, ["table", "name", "capacity"]),
    (reservations.update_reservation_status, ["reservation", "raw_status"]),
    (inventory.save_ingredient, ["ingredient", "name", "unit", "minimum", "is_active"]),
    (inventory.set_recipe, ["item", "quantities"]),
], ids=lambda v: getattr(v, "__name__", None))
def test_gap_services_without_an_actor_parameter(service, parameters):
    """Baseline finding: these services take no actor at all, so they cannot
    authorize; their routes are the only gate."""
    positional = [p for p, spec in inspect.signature(service).parameters.items()
                  if spec.kind is not inspect.Parameter.KEYWORD_ONLY]
    assert positional == parameters
    assert "actor" not in inspect.signature(service).parameters


# ======================================================================================
# Account lifecycle
# ======================================================================================


def test_only_admins_can_invite_staff(w):
    with refused(ADMIN_ONLY):
        invite_staff(w.accounts["staff"], "newcook1", "newcook1@example.com")
    user, code = invite_staff(w.accounts["admin"], "newcook2", "newcook2@example.com")
    assert user.role == Role.STAFF and code


def test_password_reset_issuance_rules(w):
    admin, staff = w.accounts["admin"], w.accounts["staff"]
    with refused(ADMIN_ONLY):
        issue_password_reset(staff, w.target_staff)
    with refused("Use Change password for your own account."):
        issue_password_reset(admin, admin)
    with refused("is deactivated. Activate the account first."):
        issue_password_reset(admin, w.target_inactive)
    assert issue_password_reset(admin, w.target_staff)


def test_gap_admin_can_issue_a_reset_code_for_another_admin(w):
    """Baseline finding (O3 Task 1, R1): any admin may issue a reset for any
    other active admin and receives the code -- which sets that admin's
    password. Peer admins have no tier between them today."""
    assert issue_password_reset(w.accounts["admin"], w.accounts["pending_admin"])


def test_activation_rules(w):
    admin, staff = w.accounts["admin"], w.accounts["staff"]
    with refused(ADMIN_ONLY):
        set_active(staff, w.target_staff, False)
    with refused("You cannot deactivate your own account."):
        set_active(admin, admin, False)
    assert set_active(admin, w.target_staff, False) is True
    assert set_active(admin, w.target_staff, False) is False           # repeat: no-op


def test_gap_admin_can_deactivate_another_admin_while_one_remains(w):
    """Baseline finding (O3 Task 1, R2): any admin may deactivate any other
    admin, limited only by 'one other active admin must remain'. The world
    has two active admins: `admin` and `pending_admin`."""
    assert set_active(w.accounts["admin"], w.accounts["pending_admin"], False) is True
    assert _reload(User, w.accounts["pending_admin"].id).is_active is False


def test_the_last_active_admin_cannot_be_deactivated(w):
    admin, peer, inactive = w.accounts["admin"], w.accounts["pending_admin"], w.accounts["inactive_admin"]
    assert set_active(admin, peer, False) is True        # `admin` is now the only active admin
    with refused("At least one other active administrator must remain."):
        set_active(inactive, admin, False)                # a different ADMIN-role actor tries to remove it
    assert _reload(User, admin.id).is_active is True


def test_gap_account_services_check_the_actor_role_but_not_that_it_is_active(w):
    """Baseline finding: _require_admin checks actor.role only. An inactive
    ADMIN actor is accepted by the service; the route gate (no session for an
    inactive account) is what stops it today."""
    inactive = w.accounts["inactive_admin"]
    assert issue_password_reset(inactive, w.target_staff)
    assert set_active(inactive, w.target_staff, False) is True


def test_gap_staff_approval_has_no_service_function():
    """Baseline finding: approving or revoking staff is implemented in the
    route itself (app/routes/admin_staff.py::_set_approval), which takes no
    actor; there is no service function to carry an independent check."""
    from app.routes import admin_staff

    assert list(inspect.signature(admin_staff._set_approval).parameters) == ["user_id", "approved"]


# ======================================================================================
# O2 organization membership services
# ======================================================================================


def test_membership_services_require_an_active_admin_actor_or_none(w):
    membership = _db.session.query(OrganizationMembership).filter_by(user_id=w.target_staff.id).one()
    for actor in (w.accounts["staff"], w.accounts["customer"], w.accounts["inactive_admin"]):
        with refused("Only an active administrator can change organization membership."):
            organization.revoke_membership(membership, actor=actor)
    assert organization.revoke_membership(membership, actor=w.accounts["admin"]) is True
    assert organization.reinstate_membership(membership) is True        # actor None: CLI/system path


def test_membership_revocation_does_not_change_access_today(app, w):
    """O2 boundary, re-pinned for O3: a revoked membership leaves sign-in and
    the role gate exactly as before."""
    membership = _db.session.query(OrganizationMembership).filter_by(user_id=w.accounts["staff"].id).one()
    organization.revoke_membership(membership)
    client = app.test_client()
    sign_in(client, w.accounts["staff"])
    assert client.get("/staff/orders", headers=HTML).status_code == 200
