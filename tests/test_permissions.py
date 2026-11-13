"""O3.2: the permission foundation (app/utils/permissions.py).

Nothing in the application uses these helpers yet; the O3.1 baseline
(tests/test_authz_baseline_*.py) still pins the real routes. These tests pin
the foundation itself and prove its compatibility grants agree with today's
route gates and service checks, so a later milestone can move a check onto a
permission without changing who gets through.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
from urllib.parse import parse_qs, urlparse

import pytest
from flask import g

from app.extensions import db as _db
from app.models.organization import OrganizationMembership
from app.models.user import Role
from app.services import inventory, organization, pricing
from app.services.accounts import invite_staff, set_active
from app.services.errors import ValidationError
from app.services.menu import set_availability
from app.services.restaurant import RestaurantInput, update_restaurant
from app.utils.permissions import (
    BRANCH_ROLE_GRANTS,
    COMPATIBILITY_GRANTS,
    ORGANIZATION_ROLE_GRANTS,
    PERMISSION_SCOPES,
    AuthContext,
    BranchRole,
    OrganizationRole,
    Permission,
    PermissionDenied,
    PermissionScope,
    auth_context_for_user,
    current_auth_context,
    has_permission,
    permission_required,
    permissions_for,
    require_permission,
)
from tests.authz_baseline_world import CLASSES, build_world, sign_in
from tests.test_authz_baseline_routes import GATES, ROUTES

P = Permission
HTML = {"Accept": "text/html"}
JSON = {"Accept": "application/json"}

STAFF_GRANTS = {
    P.ORDERS_VIEW, P.ORDERS_MANAGE, P.PAYMENTS_RECORD, P.DISCOUNTS_APPLY, P.TABLES_OPERATE,
    P.RESERVATIONS_MANAGE, P.MENU_VIEW, P.MENU_AVAILABILITY, P.INVENTORY_VIEW, P.INVENTORY_RECORD,
}
ADMIN_ONLY_GRANTS = {
    P.REFUNDS_CREATE, P.PAYMENTS_VIEW, P.SETTINGS_PRICING_MANAGE, P.BRANCH_PROFILE_MANAGE, P.TABLES_MANAGE,
    P.MENU_MANAGE, P.INVENTORY_ADJUST, P.INVENTORY_MANAGE, P.REPORTS_VIEW, P.AUDIT_VIEW,
    P.ACCOUNTS_MANAGE, P.ORGANIZATION_MEMBERS_MANAGE, P.ORGANIZATION_ADMINS_MANAGE, P.BRANCH_MEMBERS_MANAGE,
}
NEVER_GRANTED = {P.ORGANIZATION_VIEW, P.ORGANIZATION_OWNER_MANAGE}


def ctx(role: Role, *, active: bool = True, approved: bool = True, **extra) -> AuthContext:
    return AuthContext(user_id=7, global_role=role, is_active=active, staff_approved=approved, **extra)


# ======================================================================================
# 1. Catalog
# ======================================================================================


def test_catalog_identifiers_are_pinned():
    """The identifiers are stable: renaming, adding or removing one fails
    here until it is done on purpose."""
    assert sorted(p.value for p in Permission) == sorted([
        "organization.view", "organization.owner.manage", "organization.admins.manage",
        "organization.members.manage", "branch.members.manage", "branch.profile.manage",
        "tables.manage", "tables.operate", "orders.view", "orders.manage", "payments.record",
        "payments.view", "refunds.create", "discounts.apply", "settings.pricing.manage", "menu.view",
        "menu.availability", "menu.manage", "inventory.view", "inventory.record", "inventory.adjust",
        "inventory.manage", "reservations.manage", "reports.view", "audit.view", "accounts.manage",
    ])


def test_catalog_identifiers_are_unique_and_well_formed():
    values = [p.value for p in Permission]
    assert len(values) == len(set(values)) == len(Permission.__members__)  # no aliases
    for value in values:
        assert re.fullmatch(r"[a-z]+(\.[a-z]+)+", value) and len(value) <= 64, value


def test_a_duplicate_identifier_is_refused_at_definition():
    """``enum.unique`` turns a copy-pasted value into an error rather than a
    silent alias of another permission."""
    import enum

    with pytest.raises(ValueError):
        enum.unique(enum.Enum("Broken", {"A": "orders.view", "B": "orders.view"}))
    assert Permission.__members__.keys() == {p.name for p in Permission}


def test_every_permission_has_a_scope():
    assert set(PERMISSION_SCOPES) == set(Permission)
    assert all(isinstance(scope, PermissionScope) for scope in PERMISSION_SCOPES.values())
    # Both one-row local settings tables live in each branch's own database.
    assert PERMISSION_SCOPES[P.SETTINGS_PRICING_MANAGE] is PermissionScope.BRANCH
    assert PERMISSION_SCOPES[P.BRANCH_PROFILE_MANAGE] is PermissionScope.BRANCH


# ======================================================================================
# 2. AuthContext
# ======================================================================================


def test_context_is_immutable_and_hashable():
    context = ctx(Role.STAFF)
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.global_role = Role.ADMIN
    with pytest.raises((AttributeError, TypeError)):  # slots: no new attributes either
        object.__setattr__(context, "permissions", frozenset(Permission))
    assert context == ctx(Role.STAFF) and hash(context) == hash(ctx(Role.STAFF))
    assert dataclasses.replace(context, global_role=Role.ADMIN) is not context


@pytest.mark.parametrize("bad", [
    dict(user_id="7"), dict(user_id=True), dict(global_role="admin"), dict(is_active=1),
    dict(staff_approved="yes"), dict(organization_id="1"), dict(branch_id=False),
    dict(organization_role="owner"), dict(branch_role="manager"),
])
def test_context_refuses_loosely_typed_values(bad):
    fields = dict(user_id=7, global_role=Role.STAFF, is_active=True, staff_approved=True)
    fields.update(bad)
    with pytest.raises(TypeError):
        AuthContext(**fields)


@pytest.mark.parametrize("cls", [c for c in CLASSES if c != "anonymous"])
def test_context_is_built_from_the_user_row(db, cls):
    w = build_world()
    user = w.accounts[cls]
    context = auth_context_for_user(user)
    role, active, approved, _ = CLASSES[cls]
    assert context == AuthContext(user_id=user.id, global_role=role, is_active=active, staff_approved=approved)
    assert context.organization_role is None and context.branch_role is None  # no storage in O3.2
    assert context.organization_id is None and context.branch_id is None      # not resolved in O3.2
    scoped = auth_context_for_user(user, organization_id=w.organization.id, branch_id=w.branch.id)
    assert (scoped.organization_id, scoped.branch_id) == (w.organization.id, w.branch.id)


def test_current_context_follows_the_existing_session_gate(app, db):
    """current_auth_context() is get_current_user() -- including its refusal
    of inactive and pending-staff sessions -- and stores nothing in the
    session."""
    w = build_world()
    for cls in CLASSES:
        g.pop("current_user", None)  # the fixture's app context is shared (known issue #29)
        with app.test_request_context("/"):
            from flask import session

            user = w.accounts.get(cls)
            if user is not None:
                session["user_id"], session["sv"] = user.id, user.session_version
            context = current_auth_context()
            if CLASSES[cls][3] == "anonymous":
                assert context is None, cls
            else:
                assert context == auth_context_for_user(user), cls
            assert set(session) <= {"user_id", "sv"}, cls


# ======================================================================================
# 3-5. Compatibility grants per global role
# ======================================================================================


def test_admin_compatibility_grants_are_exact():
    assert COMPATIBILITY_GRANTS[Role.ADMIN] == STAFF_GRANTS | ADMIN_ONLY_GRANTS
    assert permissions_for(ctx(Role.ADMIN)) == STAFF_GRANTS | ADMIN_ONLY_GRANTS


def test_staff_compatibility_grants_are_exact():
    assert COMPATIBILITY_GRANTS[Role.STAFF] == STAFF_GRANTS
    assert permissions_for(ctx(Role.STAFF)) == STAFF_GRANTS
    for permission in ADMIN_ONLY_GRANTS | NEVER_GRANTED:
        assert not has_permission(ctx(Role.STAFF), permission), permission


def test_customers_hold_no_permission():
    assert COMPATIBILITY_GRANTS[Role.CUSTOMER] == frozenset()
    assert permissions_for(ctx(Role.CUSTOMER)) == frozenset()
    assert not any(has_permission(ctx(Role.CUSTOMER), p) for p in Permission)


def test_no_global_role_holds_owner_or_organization_view():
    for role in Role:
        assert not (COMPATIBILITY_GRANTS[role] & NEVER_GRANTED), role


def test_every_global_role_has_an_entry_and_the_tables_are_read_only():
    assert set(COMPATIBILITY_GRANTS) == set(Role)
    assert all(isinstance(grants, frozenset) for grants in COMPATIBILITY_GRANTS.values())
    with pytest.raises(TypeError):
        COMPATIBILITY_GRANTS[Role.CUSTOMER] = frozenset(Permission)
    with pytest.raises(TypeError):
        ORGANIZATION_ROLE_GRANTS[OrganizationRole.OWNER] = frozenset(Permission)


def test_pending_admin_keeps_admin_grants_as_today():
    """staff_approved is not consulted for ADMIN today (O3.1 ``pending_admin``).
    O3.2 preserves that; the pending-ADMIN policy question stays open."""
    assert permissions_for(ctx(Role.ADMIN, approved=False)) == permissions_for(ctx(Role.ADMIN))


def test_pending_staff_hold_nothing():
    assert permissions_for(ctx(Role.STAFF, approved=False)) == frozenset()


# ======================================================================================
# 6-8. Unknown identifiers, missing context, inactive accounts
# ======================================================================================


@pytest.mark.parametrize("identifier", ["orders.view", "orders.delete", "", None, 42, Role.ADMIN, object()])
def test_unknown_or_raw_identifiers_are_denied(identifier):
    """Only Permission members are evaluated -- a raw string is denied even
    when it is spelled like a real permission."""
    assert has_permission(ctx(Role.ADMIN), identifier) is False
    with pytest.raises(PermissionDenied):
        require_permission(ctx(Role.ADMIN), identifier)


@pytest.mark.parametrize("missing", [None, "admin", {"global_role": Role.ADMIN}, Role.ADMIN])
def test_a_missing_or_fake_context_is_denied(missing):
    assert permissions_for(missing) == frozenset()
    assert not any(has_permission(missing, p) for p in Permission)
    with pytest.raises(PermissionDenied):
        require_permission(missing, P.ORDERS_VIEW)


def test_a_real_user_row_is_not_a_context(db):
    """Passing the User object itself (a likely mistake) is denied, not
    read for its role."""
    w = build_world()
    assert not has_permission(w.accounts["admin"], P.AUDIT_VIEW)


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("approved", [True, False])
def test_inactive_accounts_are_denied_everything(role, approved):
    context = ctx(role, active=False, approved=approved)
    assert not context.is_eligible
    assert permissions_for(context) == frozenset()
    assert not any(has_permission(context, p) for p in Permission)


# ======================================================================================
# 9. Membership-role fields grant nothing in O3.2
# ======================================================================================


def test_membership_role_tables_are_empty():
    assert set(ORGANIZATION_ROLE_GRANTS) == set(OrganizationRole)
    assert set(BRANCH_ROLE_GRANTS) == set(BranchRole)
    assert all(grants == frozenset() for grants in ORGANIZATION_ROLE_GRANTS.values())
    assert all(grants == frozenset() for grants in BRANCH_ROLE_GRANTS.values())


@pytest.mark.parametrize("role, org_role, branch_role", list(itertools.product(
    list(Role), [None, *OrganizationRole], [None, *BranchRole])))
def test_membership_roles_never_widen_access(role, org_role, branch_role):
    """A context carrying ORGANIZATION_OWNER or BRANCH_MANAGER -- e.g. built
    by hand, or by a later milestone before its policy is approved -- holds
    exactly what its global role holds, never more."""
    context = ctx(role, organization_role=org_role, branch_role=branch_role, organization_id=1, branch_id=1)
    assert permissions_for(context) == COMPATIBILITY_GRANTS[role]
    assert not has_permission(context, P.ORGANIZATION_OWNER_MANAGE)
    inactive = dataclasses.replace(context, is_active=False)
    assert permissions_for(inactive) == frozenset()


# ======================================================================================
# 10. Enforcement helpers
# ======================================================================================


def test_require_permission_allows_and_denies():
    assert require_permission(ctx(Role.STAFF), P.ORDERS_MANAGE) is None
    with pytest.raises(PermissionDenied) as denied:
        require_permission(ctx(Role.STAFF), P.REFUNDS_CREATE)
    assert denied.value.permission is P.REFUNDS_CREATE
    assert str(denied.value) == "You do not have permission to do that."
    with pytest.raises(PermissionDenied):
        require_permission(ctx(Role.ADMIN, active=False), P.ORDERS_VIEW)


def test_permission_denied_is_not_a_validation_error():
    """Routes catch ValidationError and flash-and-redirect; a denial must
    never be absorbed that way."""
    assert not issubclass(PermissionDenied, ValidationError)


@pytest.mark.parametrize("bad", ["audit.view", None, Role.ADMIN])
def test_permission_required_refuses_a_non_permission_at_definition(bad):
    with pytest.raises(TypeError):
        permission_required(bad)


def _probe_app(app):
    """Two routes guarded only by permission_required, registered on this
    test's own app instance before its first request."""

    @permission_required(P.AUDIT_VIEW)
    def admin_probe():
        return "permission granted"

    @permission_required(P.ORDERS_VIEW)
    def staff_probe():
        return "permission granted"

    app.add_url_rule("/_test/perm/admin", "perm_admin_probe", admin_probe)
    app.add_url_rule("/_test/perm/staff", "perm_staff_probe", staff_probe)


def _shape(resp):
    where = urlparse(resp.headers.get("Location", ""))
    return resp.status_code, where.path, sorted(parse_qs(where.query)), resp.get_json(silent=True)


@pytest.mark.parametrize("cls", list(CLASSES))
@pytest.mark.parametrize("headers", [HTML, JSON], ids=["html", "json"])
@pytest.mark.parametrize("probe, legacy", [("admin", "/_test/admin"), ("staff", "/_test/staff")])
def test_permission_required_responds_exactly_like_require_role(app, db, cls, headers, probe, legacy):
    """For every account class, a permission-guarded route answers exactly
    as the matching require_role route does today (conftest's probes:
    require_role(ADMIN) and require_role(STAFF, ADMIN)): same status, same
    redirect, same JSON body."""
    _probe_app(app)
    w = build_world()
    new_client, old_client = app.test_client(), app.test_client()
    sign_in(new_client, w.accounts.get(cls))
    sign_in(old_client, w.accounts.get(cls))
    g.pop("current_user", None)  # one app context per test (known issue #29): no carry-over
    new = new_client.get(f"/_test/perm/{probe}", headers=headers)
    g.pop("current_user", None)
    old = old_client.get(legacy, headers=headers)
    new_shape, old_shape = _shape(new), _shape(old)
    if old.status_code == 200:
        assert new.status_code == 200 and new.get_data(as_text=True) == "permission granted"
    else:
        assert new_shape[:2] == old_shape[:2], (cls, new_shape, old_shape)
        assert new_shape[2] == old_shape[2], (cls, new_shape, old_shape)  # `next` carried the same way
        assert new_shape[3] == old_shape[3], (cls, new_shape, old_shape)
    with new_client.session_transaction() as session:
        # Only keys the application already uses (csrf_token: the HTML 403
        # page renders a form) -- no context, role or permission is stored.
        assert set(session) <= {"user_id", "sv", "_flashes", "locale", "csrf_token"}


# ======================================================================================
# 11. Compatibility with today's route gates and service checks
# ======================================================================================

# The permission each workforce route would check, per the approved O3 design
# (§3). Used here only to prove the compatibility grants reproduce today's
# gates; no route uses a permission yet.
ROUTE_PERMISSION = {
    "GET /staff/orders": P.ORDERS_VIEW,
    "GET /staff/orders/<int:order_id>": P.ORDERS_VIEW,
    "GET /staff/kitchen": P.ORDERS_VIEW,
    "GET /staff/orders/new": P.ORDERS_MANAGE,
    "POST /staff/orders/new": P.ORDERS_MANAGE,
    "POST /staff/orders/<int:order_id>/status": P.ORDERS_MANAGE,
    "POST /staff/orders/<int:order_id>/discount": P.DISCOUNTS_APPLY,
    "POST /staff/orders/<int:order_id>/payment": P.PAYMENTS_RECORD,
    "POST /staff/orders/<int:order_id>/payments/<int:entry_id>/refund": P.REFUNDS_CREATE,
    "GET /staff/tables": P.TABLES_OPERATE,
    "POST /staff/tables/<int:table_id>/status": P.TABLES_OPERATE,
    "GET /admin/tables/new": P.TABLES_MANAGE,
    "POST /admin/tables/new": P.TABLES_MANAGE,
    "GET /admin/tables/<int:table_id>/edit": P.TABLES_MANAGE,
    "POST /admin/tables/<int:table_id>/edit": P.TABLES_MANAGE,
    "GET /staff/reservations": P.RESERVATIONS_MANAGE,
    "POST /staff/reservations/<int:reservation_id>/status": P.RESERVATIONS_MANAGE,
    "GET /staff/menu": P.MENU_VIEW,
    "GET /admin/menu": P.MENU_VIEW,
    "POST /staff/menu/<int:item_id>/availability": P.MENU_AVAILABILITY,
    "GET /admin/menu/create": P.MENU_MANAGE,
    "POST /admin/menu/create": P.MENU_MANAGE,
    "GET /admin/menu/<int:item_id>/edit": P.MENU_MANAGE,
    "POST /admin/menu/<int:item_id>/edit": P.MENU_MANAGE,
    "GET /admin/menu/<int:item_id>/recipe": P.MENU_MANAGE,
    "POST /admin/menu/<int:item_id>/recipe": P.MENU_MANAGE,
    "GET /admin/categories": P.MENU_MANAGE,
    "GET /admin/categories/create": P.MENU_MANAGE,
    "POST /admin/categories/create": P.MENU_MANAGE,
    "GET /admin/categories/<int:category_id>/edit": P.MENU_MANAGE,
    "POST /admin/categories/<int:category_id>/edit": P.MENU_MANAGE,
    "GET /staff/inventory": P.INVENTORY_VIEW,
    "GET /staff/inventory/<int:ingredient_id>": P.INVENTORY_VIEW,
    "POST /staff/inventory/<int:ingredient_id>/movement": P.INVENTORY_RECORD,
    "GET /admin/inventory/new": P.INVENTORY_MANAGE,
    "POST /admin/inventory/new": P.INVENTORY_MANAGE,
    "GET /admin/inventory/<int:ingredient_id>/edit": P.INVENTORY_MANAGE,
    "POST /admin/inventory/<int:ingredient_id>/edit": P.INVENTORY_MANAGE,
    "GET /admin/settings": P.SETTINGS_PRICING_MANAGE,
    "POST /admin/settings": P.SETTINGS_PRICING_MANAGE,
    "GET /admin/settings/restaurant": P.BRANCH_PROFILE_MANAGE,
    "POST /admin/settings/restaurant": P.BRANCH_PROFILE_MANAGE,
    "GET /admin/payments": P.PAYMENTS_VIEW,
    "GET /admin/reports": P.REPORTS_VIEW,
    "GET /admin/audit": P.AUDIT_VIEW,
    "GET /admin/accounts": P.ACCOUNTS_MANAGE,
    "GET /admin/accounts/invite": P.ORGANIZATION_MEMBERS_MANAGE,
    "POST /admin/accounts/invite": P.ORGANIZATION_MEMBERS_MANAGE,
    "POST /admin/accounts/<int:user_id>/reset": P.ACCOUNTS_MANAGE,
    "POST /admin/accounts/<int:user_id>/deactivate": P.ACCOUNTS_MANAGE,
    "POST /admin/accounts/<int:user_id>/activate": P.ACCOUNTS_MANAGE,
    "GET /admin/staff": P.ACCOUNTS_MANAGE,
    "POST /admin/staff/<int:user_id>/approve": P.ACCOUNTS_MANAGE,
    "POST /admin/staff/<int:user_id>/revoke": P.ACCOUNTS_MANAGE,
}
WORKFORCE_GATES = {"staff", "admin"}


def test_every_workforce_route_has_a_permission():
    """Exactly the O3.1 rows gated by require_role(STAFF, ADMIN) or
    require_role(ADMIN) -- no more, no fewer."""
    assert set(ROUTE_PERMISSION) == {r.id for r in ROUTES if r.gate in WORKFORCE_GATES}


@pytest.mark.parametrize("cls", list(CLASSES))
@pytest.mark.parametrize("route", [r for r in ROUTES if r.gate in WORKFORCE_GATES], ids=lambda r: r.id)
def test_compatibility_grants_reproduce_the_pinned_route_gates(route, cls):
    """For every workforce route and every account class: the class gets
    the route's permission exactly when O3.1 pins the route as allowed for
    it. Inactive accounts and pending staff (no session today) hold nothing;
    pending admins hold the admin set, as today."""
    role, active, approved, effective = CLASSES[cls]
    context = None if role is None else ctx(role, active=active, approved=approved)
    allowed_today = GATES[route.gate][effective] == "ok"
    assert has_permission(context, ROUTE_PERMISSION[route.id]) is allowed_today


@pytest.mark.parametrize("role", list(Role))
def test_compatibility_grants_reproduce_the_service_role_checks(db, role):
    """The services that check the actor's role themselves today, called for
    real: each call succeeds exactly when the actor's context holds the
    matching permission."""
    w = build_world()
    actor = {Role.CUSTOMER: w.accounts["customer"], Role.STAFF: w.accounts["staff"],
             Role.ADMIN: w.accounts["admin"]}[role]
    context = auth_context_for_user(actor)
    membership = _db.session.query(OrganizationMembership).filter_by(user_id=w.target_staff.id).one()
    calls = [
        (P.MENU_AVAILABILITY, lambda: set_availability(actor, w.item.id, False)),
        (P.DISCOUNTS_APPLY, lambda: pricing.apply_discount(w.order, "percent", "5", "x", actor)),
        (P.INVENTORY_ADJUST, lambda: inventory.record_movement(w.ingredient, "adjustment", "1", "count", actor)),
        (P.BRANCH_PROFILE_MANAGE, lambda: update_restaurant(actor, RestaurantInput("Cafe", None, None, None,
                                                                                   None, "en"))),
        (P.ORGANIZATION_MEMBERS_MANAGE, lambda: invite_staff(actor, f"cook{role.value}", f"c{role.value}@example.com")),
        (P.ORGANIZATION_MEMBERS_MANAGE, lambda: organization.revoke_membership(membership, actor=actor)),
        (P.ACCOUNTS_MANAGE, lambda: set_active(actor, w.target_inactive, True)),
    ]
    for permission, call in calls:
        try:
            call()
            succeeded = True
        except ValidationError:
            _db.session.rollback()
            succeeded = False
        assert succeeded is has_permission(context, permission), (role, permission)
