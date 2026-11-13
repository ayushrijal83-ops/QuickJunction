"""Permission foundation (O3.2): the permission catalog, an immutable
authorization context, one evaluation function and reusable enforcement
helpers.

**Nothing uses this module yet.** Every route and service still authorizes
through ``require_role`` / ``login_required`` / ``customer_required``
(app/utils/authorization.py) and its own role checks, exactly as the O3.1
baseline (tests/test_authz_baseline_*.py) pins. Later O3 milestones move
routes and services onto these helpers; this one only provides them.

Three ideas are kept apart:

- **The catalog** (``Permission``) names what can be authorized. Defining a
  permission grants it to nobody.
- **Compatibility grants** (``COMPATIBILITY_GRANTS``) say which permissions
  each *global* role (``users.role``) holds **today**, derived from the
  current route gates and service checks. They are not the final O3 role
  policy; they exist so a check written with a permission behaves exactly
  like the role check it replaces.
- **Membership roles** (``OrganizationRole``, ``BranchRole``) are the
  approved O3 roles. They have no storage yet, ``auth_context_for_user``
  never sets them, and they grant **nothing** in O3.2
  (``ORGANIZATION_ROLE_GRANTS`` / ``BRANCH_ROLE_GRANTS`` are empty), so a
  value placed in a context by hand can never raise anyone's access.

The context is built from the server-side ``User`` row on every use. It is
never stored in the session, and no role, organization or branch is ever
taken from request data.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from functools import wraps
from types import MappingProxyType
from typing import Any, Callable, Mapping

from app.models.user import Role, User
from app.utils.authorization import _forbidden, _unauthorized, get_current_user

# --- catalog -------------------------------------------------------------------------------------


@enum.unique
class Permission(str, enum.Enum):
    """Every permission the application can check. The values are stable
    identifiers: they will be referenced from code, tests and audit data, so
    renaming one is a deliberate, reviewed change (tests pin the full list).
    ``enum.unique`` makes a duplicated value an import-time error instead of a
    silent alias."""

    # Organization
    ORGANIZATION_VIEW = "organization.view"
    ORGANIZATION_OWNER_MANAGE = "organization.owner.manage"
    ORGANIZATION_ADMINS_MANAGE = "organization.admins.manage"
    ORGANIZATION_MEMBERS_MANAGE = "organization.members.manage"
    # Branch
    BRANCH_MEMBERS_MANAGE = "branch.members.manage"
    BRANCH_PROFILE_MANAGE = "branch.profile.manage"
    TABLES_MANAGE = "tables.manage"
    TABLES_OPERATE = "tables.operate"
    # Orders and money
    ORDERS_VIEW = "orders.view"
    ORDERS_MANAGE = "orders.manage"
    PAYMENTS_RECORD = "payments.record"
    PAYMENTS_VIEW = "payments.view"
    REFUNDS_CREATE = "refunds.create"
    DISCOUNTS_APPLY = "discounts.apply"
    # The pricing settings at /admin/settings: the tax rate and the staff
    # discount cap (the one-row ``pricing_settings`` table). Not item prices
    # (MENU_MANAGE) and not the restaurant profile (BRANCH_PROFILE_MANAGE);
    # there is no general settings capability to name.
    SETTINGS_PRICING_MANAGE = "settings.pricing.manage"
    # Menu
    MENU_VIEW = "menu.view"
    MENU_AVAILABILITY = "menu.availability"
    MENU_MANAGE = "menu.manage"
    # Inventory
    INVENTORY_VIEW = "inventory.view"
    INVENTORY_RECORD = "inventory.record"
    INVENTORY_ADJUST = "inventory.adjust"
    INVENTORY_MANAGE = "inventory.manage"
    # Front of house
    RESERVATIONS_MANAGE = "reservations.manage"
    # Oversight and accounts
    REPORTS_VIEW = "reports.view"
    AUDIT_VIEW = "audit.view"
    ACCOUNTS_MANAGE = "accounts.manage"


class PermissionScope(str, enum.Enum):
    """Where a permission applies once organization and branch roles exist:
    across the organization, or at one branch. With one branch per
    installation (O1) the two behave identically; nothing evaluates scope in
    O3.2."""

    ORGANIZATION = "organization"
    BRANCH = "branch"


_ORG = PermissionScope.ORGANIZATION
_BRANCH = PermissionScope.BRANCH

PERMISSION_SCOPES: Mapping[Permission, PermissionScope] = MappingProxyType({
    Permission.ORGANIZATION_VIEW: _ORG,
    Permission.ORGANIZATION_OWNER_MANAGE: _ORG,
    Permission.ORGANIZATION_ADMINS_MANAGE: _ORG,
    Permission.ORGANIZATION_MEMBERS_MANAGE: _ORG,
    Permission.BRANCH_MEMBERS_MANAGE: _BRANCH,
    Permission.BRANCH_PROFILE_MANAGE: _BRANCH,
    Permission.TABLES_MANAGE: _BRANCH,
    Permission.TABLES_OPERATE: _BRANCH,
    Permission.ORDERS_VIEW: _BRANCH,
    Permission.ORDERS_MANAGE: _BRANCH,
    Permission.PAYMENTS_RECORD: _BRANCH,
    Permission.PAYMENTS_VIEW: _BRANCH,
    Permission.REFUNDS_CREATE: _BRANCH,
    Permission.DISCOUNTS_APPLY: _BRANCH,
    # Branch: like the restaurant profile, the pricing_settings row lives in
    # each branch's own database and applies only to that branch's orders.
    Permission.SETTINGS_PRICING_MANAGE: _BRANCH,
    Permission.MENU_VIEW: _BRANCH,
    Permission.MENU_AVAILABILITY: _BRANCH,
    Permission.MENU_MANAGE: _ORG,
    Permission.INVENTORY_VIEW: _BRANCH,
    Permission.INVENTORY_RECORD: _BRANCH,
    Permission.INVENTORY_ADJUST: _BRANCH,
    Permission.INVENTORY_MANAGE: _ORG,
    Permission.RESERVATIONS_MANAGE: _BRANCH,
    Permission.REPORTS_VIEW: _BRANCH,
    Permission.AUDIT_VIEW: _ORG,
    Permission.ACCOUNTS_MANAGE: _ORG,
})


# --- membership roles (approved O3 roles; no storage and no grants in O3.2) ---------------------


class OrganizationRole(str, enum.Enum):
    OWNER = "owner"   # ORGANIZATION_OWNER
    ADMIN = "admin"   # ORGANIZATION_ADMIN


class BranchRole(str, enum.Enum):
    MANAGER = "manager"   # BRANCH_MANAGER
    STAFF = "staff"       # BRANCH_STAFF


# Deliberately empty: the membership-role policy is not enforced until a
# later O3 milestone stores and backfills these roles. Until then a role in
# a context must not widen access, so each maps to no permission at all.
ORGANIZATION_ROLE_GRANTS: Mapping[OrganizationRole, frozenset[Permission]] = MappingProxyType(
    {role: frozenset() for role in OrganizationRole})
BRANCH_ROLE_GRANTS: Mapping[BranchRole, frozenset[Permission]] = MappingProxyType(
    {role: frozenset() for role in BranchRole})


# --- compatibility grants (today's behaviour, per global role) -----------------------------------

# Each entry is a capability the role has today, through a route gate or a
# service check. Not the final O3 policy -- see the module docstring.
_STAFF_GRANTS = frozenset({
    Permission.ORDERS_VIEW,           # /staff/orders, /staff/orders/<id>, /staff/kitchen
    Permission.ORDERS_MANAGE,         # /staff/orders/new, /staff/orders/<id>/status
    Permission.PAYMENTS_RECORD,       # /staff/orders/<id>/payment
    Permission.DISCOUNTS_APPLY,       # /staff/orders/<id>/discount (capped by the service)
    Permission.TABLES_OPERATE,        # /staff/tables, /staff/tables/<id>/status
    Permission.RESERVATIONS_MANAGE,   # /staff/reservations, /staff/reservations/<id>/status
    Permission.MENU_VIEW,             # /staff/menu, GET /admin/menu
    Permission.MENU_AVAILABILITY,     # /staff/menu/<id>/availability
    Permission.INVENTORY_VIEW,        # /staff/inventory, /staff/inventory/<id>
    Permission.INVENTORY_RECORD,      # /staff/inventory/<id>/movement (not adjustments)
})

_ADMIN_GRANTS = _STAFF_GRANTS | frozenset({
    Permission.REFUNDS_CREATE,               # /staff/orders/<id>/payments/<eid>/refund
    Permission.PAYMENTS_VIEW,                # /admin/payments
    Permission.SETTINGS_PRICING_MANAGE,      # /admin/settings (tax rate, staff discount cap)
    Permission.BRANCH_PROFILE_MANAGE,        # /admin/settings/restaurant, update_restaurant()
    Permission.TABLES_MANAGE,                # /admin/tables/new, /admin/tables/<id>/edit
    Permission.MENU_MANAGE,                  # /admin/menu/*, /admin/categories/*
    Permission.INVENTORY_ADJUST,             # adjustment movements (record_movement)
    Permission.INVENTORY_MANAGE,             # /admin/inventory/*
    Permission.REPORTS_VIEW,                 # /admin/reports, admin dashboard figures
    Permission.AUDIT_VIEW,                   # /admin/audit
    Permission.ACCOUNTS_MANAGE,              # /admin/accounts reset/activate/deactivate, /admin/staff
    Permission.ORGANIZATION_MEMBERS_MANAGE,  # invite_staff(); O2 membership services
    Permission.ORGANIZATION_ADMINS_MANAGE,   # an admin may reset/deactivate another admin today
    Permission.BRANCH_MEMBERS_MANAGE,        # O2 assign/revoke/reinstate assignment services
})
# Not granted to any global role: ORGANIZATION_VIEW (no such capability
# exists today) and ORGANIZATION_OWNER_MANAGE (no owner exists).

COMPATIBILITY_GRANTS: Mapping[Role, frozenset[Permission]] = MappingProxyType({
    Role.ADMIN: _ADMIN_GRANTS,
    Role.STAFF: _STAFF_GRANTS,
    Role.CUSTOMER: frozenset(),   # customers use ownership checks, never permissions
})


# --- context ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The authenticated actor, as the server knows it. Immutable and
    hashable; built from the ``User`` row, never from request data, and never
    stored in the session.

    ``organization_id`` / ``branch_id`` / ``organization_role`` /
    ``branch_role`` are placeholders for later O3 milestones: they are
    ``None`` unless a caller passes them, and no permission depends on them
    in O3.2."""

    user_id: int
    global_role: Role
    is_active: bool
    staff_approved: bool
    organization_id: int | None = None
    branch_id: int | None = None
    organization_role: OrganizationRole | None = None
    branch_role: BranchRole | None = None

    def __post_init__(self) -> None:
        # Type checks, so a context can never be half-built from loose values
        # (a role name string, a truthy int) and evaluated as something else.
        if not _is_int(self.user_id):
            raise TypeError("user_id must be an int")
        if not isinstance(self.global_role, Role):
            raise TypeError("global_role must be a Role")
        if type(self.is_active) is not bool or type(self.staff_approved) is not bool:
            raise TypeError("is_active and staff_approved must be bool")
        for name in ("organization_id", "branch_id"):
            value = getattr(self, name)
            if value is not None and not _is_int(value):
                raise TypeError(f"{name} must be an int or None")
        if self.organization_role is not None and not isinstance(self.organization_role, OrganizationRole):
            raise TypeError("organization_role must be an OrganizationRole or None")
        if self.branch_role is not None and not isinstance(self.branch_role, BranchRole):
            raise TypeError("branch_role must be a BranchRole or None")

    @property
    def is_eligible(self) -> bool:
        """Whether this account may act at all -- exactly the rule
        ``get_current_user`` applies today: active, and not a STAFF account
        awaiting approval. ``staff_approved`` is not consulted for ADMIN
        (the open pending-ADMIN question; O3.1 pins this behaviour)."""
        return self.is_active and not (self.global_role == Role.STAFF and not self.staff_approved)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def auth_context_for_user(user: User, *, organization_id: int | None = None,
                          branch_id: int | None = None) -> AuthContext:
    """The context for ``user``, from its database row. Organization and
    branch ids are taken only from the caller (server-side code), never
    looked up from request data; O3.2 does not resolve them itself, and no
    membership role is read because none is stored yet."""
    return AuthContext(
        user_id=user.id,
        global_role=user.role,
        is_active=bool(user.is_active),
        staff_approved=bool(user.staff_approved),
        organization_id=organization_id,
        branch_id=branch_id,
    )


def current_auth_context() -> AuthContext | None:
    """The context for the signed-in user of this request, or ``None``. Uses
    ``get_current_user``, so the session, deactivation, pending-staff and
    session-version rules apply exactly as they do today."""
    user = get_current_user()
    return None if user is None else auth_context_for_user(user)


# --- evaluation -------------------------------------------------------------------------------------


def permissions_for(context: AuthContext | None) -> frozenset[Permission]:
    """Every permission ``context`` holds. Empty for a missing context or an
    ineligible (inactive / pending-staff) account."""
    if not isinstance(context, AuthContext) or not context.is_eligible:
        return frozenset()
    granted = COMPATIBILITY_GRANTS.get(context.global_role, frozenset())
    # Membership roles: kept separate from the compatibility mapping, and
    # empty in O3.2 (see ORGANIZATION_ROLE_GRANTS).
    if context.organization_role is not None:
        granted = granted | ORGANIZATION_ROLE_GRANTS[context.organization_role]
    if context.branch_role is not None:
        granted = granted | BRANCH_ROLE_GRANTS[context.branch_role]
    return granted


def has_permission(context: AuthContext | None, permission: Permission) -> bool:
    """True only if ``context`` is a real, eligible ``AuthContext`` and
    ``permission`` is a ``Permission`` member it holds. Everything else --
    no context, an unknown identifier, a raw string (even one spelled like a
    real permission), an inactive or pending-staff account -- is False."""
    if not isinstance(permission, Permission):
        return False
    return permission in permissions_for(context)


# --- enforcement -----------------------------------------------------------------------------------


class PermissionDenied(Exception):
    """Raised by ``require_permission``. Deliberately **not** a
    ``ValidationError``: routes catch those and flash-and-redirect, which
    would make a denial look like an ordinary business-rule refusal."""

    message = "You do not have permission to do that."

    def __init__(self, permission: Any) -> None:
        super().__init__(self.message)
        self.permission = permission


def require_permission(context: AuthContext | None, permission: Permission) -> None:
    """For services: return normally if allowed, else raise
    ``PermissionDenied``."""
    if not has_permission(context, permission):
        raise PermissionDenied(permission)


def permission_required(permission: Permission) -> Callable[[Callable], Callable]:
    """For routes: like ``require_role``, with the same responses -- an
    anonymous (or signed-out-by-the-gate) request gets the login redirect /
    JSON 401, a signed-in request without the permission gets the HTML /
    JSON 403. An identifier that is not a ``Permission`` is a programming
    error, refused when the route is defined rather than denied at runtime."""
    if not isinstance(permission, Permission):
        raise TypeError(f"permission_required needs a Permission, not {permission!r}")

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            context = current_auth_context()
            if context is None:
                return _unauthorized()
            if not has_permission(context, permission):
                return _forbidden()
            return view(*args, **kwargs)

        return wrapped

    return decorator
