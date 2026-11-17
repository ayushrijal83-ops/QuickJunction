"""Workforce permission policy (O3.5): what each approved O3 membership role
is *meant* to hold, written down and checked -- and **inert**.

**Nothing reads this module.** No route, service, decorator or evaluation
function imports it, and ``app/utils/permissions.py`` does not either. Every
authorization decision is still made by ``permissions_for()`` from
``COMPATIBILITY_GRANTS`` (and the role grant maps, which stay empty), exactly
as before O3.5. ``tests/test_role_matrix.py`` proves that importing this
module changes no grant, map or answer.

What the module holds:

- ``ORGANIZATION_ROLE_POLICY`` (OWNER, ADMIN) and ``BRANCH_ROLE_POLICY``
  (MANAGER, STAFF): one immutable ``RolePolicy`` per role, listing **every**
  ``Permission`` as either allowed or denied, plus the role's discount cap.
  Both lists are written out in full so each role can be reviewed on its
  own; nothing is derived by set arithmetic.
- ``POLICY_RESTRICTIONS``: rules a static grant cannot express (who the
  target is, whether an Owner exists, CLI-only workflows, the discount cap).
  Each names its affected actors, protected targets, permitted actors,
  condition and effect as data, and is marked ``Enforcement.NOT_ENFORCED``:
  holding a permission listed in a restriction is **not** limited by that
  restriction anywhere in the application yet. A restriction about a target
  must cover all four target-sensitive permissions, so none can be bypassed
  through another.

The names deliberately differ from ``ORGANIZATION_ROLE_GRANTS`` /
``BRANCH_ROLE_GRANTS`` (the runtime maps ``permissions_for()`` reads). Moving
this policy into the runtime -- and deciding whether role grants then add to
or replace the global-role compatibility grants, which ``users.role`` an
Owner or a Manager holds, and how the restrictions are enforced -- belongs
to later O3 milestones. Until then the O3.4 resolver keeps refusing a
stored ``owner`` (``unexpected_owner``) and ``manager`` (``role_mismatch``).

The policy is checked when the module is imported; a definition that is
incomplete, contradictory, out of scope or claims to be enforced raises
``PolicyDefinitionError`` (an explicit ``raise``, not ``assert``, so it
also holds under ``python -O``). Adding a 27th ``Permission`` therefore
fails the import until every role classifies it.

Customers are not part of this policy: they are not workforce members and
hold no permission (ownership checks govern them).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.utils.permissions import (
    PERMISSION_SCOPES,
    BranchRole,
    OrganizationRole,
    Permission,
    PermissionScope,
)

P = Permission


class PolicyDefinitionError(Exception):
    """The policy below is malformed. Raised at import time (or when a
    policy object is built by hand with bad values)."""


class DiscountCap(str, enum.Enum):
    """The most a role may discount, as today's rules state it
    (``app/services/pricing.py`` ``apply_discount``)."""

    ADMIN_MAXIMUM = "admin_maximum"   # 100 % of the subtotal -- today's ADMIN rule
    STAFF_CAP = "staff_cap"           # the staff cap in pricing_settings -- today's STAFF rule


class Enforcement(str, enum.Enum):
    """Whether a restriction is enforced. O3.5 has one value on purpose:
    none of the restrictions is enforced anywhere."""

    NOT_ENFORCED = "not_enforced"


class RestrictionEffect(str, enum.Enum):
    """What a restriction says, so that who is limited, who is protected
    and who keeps the authority never has to be inferred from prose.

    The first three are **target-aware**: they concern an actor using
    ``permissions`` against an account that holds one of the
    ``protected_targets`` roles."""

    # While ``condition`` holds, the ``affected_actors`` may not use the
    # permissions against the ``protected_targets``.
    ACTOR_DENIED = "actor_denied"
    # The ``affected_actors`` may use the permissions against the
    # ``protected_targets`` only while ``condition`` holds.
    ACTOR_ALLOWED_ONLY_WHILE = "actor_allowed_only_while"
    # While ``condition`` holds, only the ``permitted_actors`` may use the
    # permissions against the ``protected_targets``; the ``affected_actors``
    # may not, although their static grant includes those permissions.
    EXCLUSIVE_TO_PERMITTED_ACTORS = "exclusive_to_permitted_actors"
    # Not target-aware: the ``affected_actors``' permission is reserved and
    # exercised only through a CLI workflow.
    CLI_ONLY = "cli_only"
    # Not target-aware: the ``affected_actors``' discounts are capped at
    # the staff discount cap.
    DISCOUNT_CAP = "discount_cap"


class RestrictionCondition(str, enum.Enum):
    """The organization state a restriction depends on."""

    ALWAYS = "always"
    NO_OWNER_EXISTS = "no_owner_exists"
    OWNER_EXISTS = "owner_exists"


TARGET_AWARE_EFFECTS: frozenset[RestrictionEffect] = frozenset({
    RestrictionEffect.ACTOR_DENIED,
    RestrictionEffect.ACTOR_ALLOWED_ONLY_WHILE,
    RestrictionEffect.EXCLUSIVE_TO_PERMITTED_ACTORS,
})

# The four permissions through which one workforce member can act on another
# account or membership (reset, deactivate, approve, revoke, re-role). A
# target-aware restriction must cover **all four**: one that named fewer
# could be bypassed through the others.
TARGET_SENSITIVE_PERMISSIONS: frozenset[Permission] = frozenset({
    P.ACCOUNTS_MANAGE,
    P.ORGANIZATION_ADMINS_MANAGE,
    P.ORGANIZATION_MEMBERS_MANAGE,
    P.BRANCH_MEMBERS_MANAGE,
})


@dataclass(frozen=True, slots=True)
class RolePolicy:
    """One role's complete classification: every ``Permission`` is in
    exactly one of ``allowed`` and ``denied``."""

    allowed: frozenset[Permission]
    denied: frozenset[Permission]
    discount_cap: DiscountCap

    def __post_init__(self) -> None:
        for name in ("allowed", "denied"):
            value = getattr(self, name)
            if type(value) is not frozenset:
                raise PolicyDefinitionError(f"{name} must be a frozenset")
            strays = [p for p in value if not isinstance(p, Permission)]
            if strays:
                raise PolicyDefinitionError(f"{name} holds non-Permission values: {strays!r}")
        if not isinstance(self.discount_cap, DiscountCap):
            raise PolicyDefinitionError("discount_cap must be a DiscountCap")
        overlap = self.allowed & self.denied
        if overlap:
            raise PolicyDefinitionError(f"allowed and denied overlap: {_names(overlap)}")
        unclassified = frozenset(Permission) - self.allowed - self.denied
        if unclassified:
            raise PolicyDefinitionError(f"unclassified permissions: {_names(unclassified)}")


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRestriction:
    """A rule a static grant cannot express. Metadata only: ``enforcement``
    is always ``NOT_ENFORCED`` in O3.5.

    - ``affected_actors``: the roles whose use of ``permissions`` the rule
      limits. They hold those permissions in their static policy; the rule
      says when that is not enough.
    - ``protected_targets``: the roles of the account being acted on (empty
      unless the effect is target-aware).
    - ``permitted_actors``: the roles that keep the authority over the
      targets (only for ``EXCLUSIVE_TO_PERMITTED_ACTORS``).
    - ``condition``: the organization state the rule depends on.
    - ``effect``: how the fields above combine (see ``RestrictionEffect``).
    - ``rule``: the same statement in prose, for readers.
    """

    code: str
    effect: RestrictionEffect
    permissions: frozenset[Permission]
    affected_actors: frozenset[OrganizationRole | BranchRole]
    rule: str
    protected_targets: frozenset[OrganizationRole | BranchRole] = frozenset()
    permitted_actors: frozenset[OrganizationRole | BranchRole] = frozenset()
    condition: RestrictionCondition = RestrictionCondition.ALWAYS
    enforcement: Enforcement = Enforcement.NOT_ENFORCED

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise PolicyDefinitionError("code must be a non-empty str")
        if not isinstance(self.rule, str) or not self.rule:
            raise PolicyDefinitionError(f"{self.code}: rule must be a non-empty str")
        if not isinstance(self.effect, RestrictionEffect):
            raise PolicyDefinitionError(f"{self.code}: effect must be a RestrictionEffect")
        if not isinstance(self.condition, RestrictionCondition):
            raise PolicyDefinitionError(f"{self.code}: condition must be a RestrictionCondition")
        if self.enforcement is not Enforcement.NOT_ENFORCED:
            raise PolicyDefinitionError(f"{self.code}: no restriction is enforced in O3.5")
        if type(self.permissions) is not frozenset or not self.permissions or not all(
                isinstance(p, Permission) for p in self.permissions):
            raise PolicyDefinitionError(f"{self.code}: permissions must be a non-empty frozenset of Permission")
        for name in ("affected_actors", "protected_targets", "permitted_actors"):
            value = getattr(self, name)
            if type(value) is not frozenset or not all(
                    isinstance(r, (OrganizationRole, BranchRole)) for r in value):
                raise PolicyDefinitionError(f"{self.code}: {name} must be a frozenset of membership roles")
        if not self.affected_actors:
            raise PolicyDefinitionError(f"{self.code}: a restriction must name the actors it affects")

        exclusive = self.effect is RestrictionEffect.EXCLUSIVE_TO_PERMITTED_ACTORS
        if self.effect in TARGET_AWARE_EFFECTS:
            if not self.protected_targets:
                raise PolicyDefinitionError(f"{self.code}: a target-aware restriction must name its protected targets")
            if self.permissions != TARGET_SENSITIVE_PERMISSIONS:
                raise PolicyDefinitionError(
                    f"{self.code}: a target-aware restriction must cover every target-sensitive permission, "
                    f"not {_names(self.permissions)} (the others would bypass it)")
        else:
            if self.protected_targets:
                raise PolicyDefinitionError(f"{self.code}: {self.effect.value} is not target-aware")
            if self.condition is not RestrictionCondition.ALWAYS:
                raise PolicyDefinitionError(f"{self.code}: {self.effect.value} does not depend on a condition")
        if exclusive:
            if not self.permitted_actors:
                raise PolicyDefinitionError(f"{self.code}: an exclusive restriction must name its permitted actors")
            if self.permitted_actors & self.affected_actors:
                raise PolicyDefinitionError(f"{self.code}: an actor cannot be both permitted and affected")
        elif self.permitted_actors:
            raise PolicyDefinitionError(f"{self.code}: only an exclusive restriction names permitted actors")
        if (self.effect is RestrictionEffect.ACTOR_ALLOWED_ONLY_WHILE
                and self.condition is RestrictionCondition.ALWAYS):
            raise PolicyDefinitionError(f"{self.code}: 'allowed only while' needs a real condition")


def _names(permissions) -> list[str]:
    return sorted(p.value if isinstance(p, Permission) else repr(p) for p in permissions)


# --- organization roles -----------------------------------------------------------------------

_OWNER = RolePolicy(
    allowed=frozenset({
        # organization
        P.ORGANIZATION_VIEW, P.ORGANIZATION_OWNER_MANAGE, P.ORGANIZATION_ADMINS_MANAGE,
        P.ORGANIZATION_MEMBERS_MANAGE, P.MENU_MANAGE, P.INVENTORY_MANAGE, P.AUDIT_VIEW, P.ACCOUNTS_MANAGE,
        # branch
        P.BRANCH_MEMBERS_MANAGE, P.BRANCH_PROFILE_MANAGE, P.TABLES_MANAGE, P.TABLES_OPERATE,
        P.ORDERS_VIEW, P.ORDERS_MANAGE, P.PAYMENTS_RECORD, P.PAYMENTS_VIEW, P.REFUNDS_CREATE,
        P.DISCOUNTS_APPLY, P.SETTINGS_PRICING_MANAGE, P.MENU_VIEW, P.MENU_AVAILABILITY,
        P.INVENTORY_VIEW, P.INVENTORY_RECORD, P.INVENTORY_ADJUST, P.RESERVATIONS_MANAGE, P.REPORTS_VIEW,
    }),
    denied=frozenset(),
    discount_cap=DiscountCap.ADMIN_MAXIMUM,
)

_ADMIN = RolePolicy(
    allowed=frozenset({
        # organization
        P.ORGANIZATION_VIEW, P.ORGANIZATION_ADMINS_MANAGE, P.ORGANIZATION_MEMBERS_MANAGE,
        P.MENU_MANAGE, P.INVENTORY_MANAGE, P.AUDIT_VIEW, P.ACCOUNTS_MANAGE,
        # branch
        P.BRANCH_MEMBERS_MANAGE, P.BRANCH_PROFILE_MANAGE, P.TABLES_MANAGE, P.TABLES_OPERATE,
        P.ORDERS_VIEW, P.ORDERS_MANAGE, P.PAYMENTS_RECORD, P.PAYMENTS_VIEW, P.REFUNDS_CREATE,
        P.DISCOUNTS_APPLY, P.SETTINGS_PRICING_MANAGE, P.MENU_VIEW, P.MENU_AVAILABILITY,
        P.INVENTORY_VIEW, P.INVENTORY_RECORD, P.INVENTORY_ADJUST, P.RESERVATIONS_MANAGE, P.REPORTS_VIEW,
    }),
    denied=frozenset({
        P.ORGANIZATION_OWNER_MANAGE,   # Owner-only authority
    }),
    discount_cap=DiscountCap.ADMIN_MAXIMUM,
)

# --- branch roles (branch-scoped permissions only) ------------------------------------------------

_MANAGER = RolePolicy(
    allowed=frozenset({
        # the Branch Staff set
        P.TABLES_OPERATE, P.ORDERS_VIEW, P.ORDERS_MANAGE, P.PAYMENTS_RECORD, P.DISCOUNTS_APPLY,
        P.RESERVATIONS_MANAGE, P.MENU_VIEW, P.MENU_AVAILABILITY, P.INVENTORY_VIEW, P.INVENTORY_RECORD,
        # manager additions
        P.TABLES_MANAGE, P.PAYMENTS_VIEW, P.REPORTS_VIEW, P.INVENTORY_ADJUST,
    }),
    denied=frozenset({
        # organization scope: never held by a branch role
        P.ORGANIZATION_VIEW, P.ORGANIZATION_OWNER_MANAGE, P.ORGANIZATION_ADMINS_MANAGE,
        P.ORGANIZATION_MEMBERS_MANAGE,   # no inviting members
        P.ACCOUNTS_MANAGE, P.MENU_MANAGE, P.INVENTORY_MANAGE, P.AUDIT_VIEW,
        # branch scope, denied by decision
        P.REFUNDS_CREATE,                # no refunds
        P.BRANCH_MEMBERS_MANAGE,         # no assigning members
        P.BRANCH_PROFILE_MANAGE,         # no customer-facing branch profile
        P.SETTINGS_PRICING_MANAGE,       # could otherwise raise the staff cap that limits it
    }),
    discount_cap=DiscountCap.STAFF_CAP,
)

_STAFF = RolePolicy(
    allowed=frozenset({
        P.TABLES_OPERATE, P.ORDERS_VIEW, P.ORDERS_MANAGE, P.PAYMENTS_RECORD, P.DISCOUNTS_APPLY,
        P.RESERVATIONS_MANAGE, P.MENU_VIEW, P.MENU_AVAILABILITY, P.INVENTORY_VIEW, P.INVENTORY_RECORD,
    }),
    denied=frozenset({
        P.ORGANIZATION_VIEW, P.ORGANIZATION_OWNER_MANAGE, P.ORGANIZATION_ADMINS_MANAGE,
        P.ORGANIZATION_MEMBERS_MANAGE, P.ACCOUNTS_MANAGE, P.MENU_MANAGE, P.INVENTORY_MANAGE, P.AUDIT_VIEW,
        P.BRANCH_MEMBERS_MANAGE, P.BRANCH_PROFILE_MANAGE, P.TABLES_MANAGE, P.PAYMENTS_VIEW,
        P.REFUNDS_CREATE, P.SETTINGS_PRICING_MANAGE, P.INVENTORY_ADJUST, P.REPORTS_VIEW,
    }),
    discount_cap=DiscountCap.STAFF_CAP,
)

ORGANIZATION_ROLE_POLICY: Mapping[OrganizationRole, RolePolicy] = MappingProxyType({
    OrganizationRole.OWNER: _OWNER,
    OrganizationRole.ADMIN: _ADMIN,
})
BRANCH_ROLE_POLICY: Mapping[BranchRole, RolePolicy] = MappingProxyType({
    BranchRole.MANAGER: _MANAGER,
    BranchRole.STAFF: _STAFF,
})


# --- restrictions a static grant cannot express (none enforced) ----------------------------------

POLICY_RESTRICTIONS: tuple[PolicyRestriction, ...] = (
    # R1 -- actor: Admin; target: Owner; always.
    PolicyRestriction(
        code="admin_cannot_act_on_owner",
        effect=RestrictionEffect.ACTOR_DENIED,
        permissions=TARGET_SENSITIVE_PERMISSIONS,
        affected_actors=frozenset({OrganizationRole.ADMIN}),
        protected_targets=frozenset({OrganizationRole.OWNER}),
        condition=RestrictionCondition.ALWAYS,
        rule="An Organization Admin may not manage, reset, deactivate, revoke, transfer, remove or otherwise "
             "act against the Organization Owner through any of these four permissions.",
    ),
    # R2 -- actor: Admin; target: another Admin; allowed only while no Owner exists.
    PolicyRestriction(
        code="admins_manage_admins_only_without_owner",
        effect=RestrictionEffect.ACTOR_ALLOWED_ONLY_WHILE,
        permissions=TARGET_SENSITIVE_PERMISSIONS,
        affected_actors=frozenset({OrganizationRole.ADMIN}),
        protected_targets=frozenset({OrganizationRole.ADMIN}),
        condition=RestrictionCondition.NO_OWNER_EXISTS,
        rule="An Organization Admin may act on another Organization Admin through these four permissions "
             "only while the organization has no Owner.",
    ),
    # R3 -- the other side of R2: once an Owner exists, the Owner alone manages
    # Admin targets, and an Admin may not reach them through any of the four.
    PolicyRestriction(
        code="only_owner_manages_admins_when_owner_exists",
        effect=RestrictionEffect.EXCLUSIVE_TO_PERMITTED_ACTORS,
        permissions=TARGET_SENSITIVE_PERMISSIONS,
        affected_actors=frozenset({OrganizationRole.ADMIN}),
        permitted_actors=frozenset({OrganizationRole.OWNER}),
        protected_targets=frozenset({OrganizationRole.ADMIN}),
        condition=RestrictionCondition.OWNER_EXISTS,
        rule="Once the organization has an Owner, only the Organization Owner may manage Organization Admin "
             "targets through these four permissions; an Organization Admin may not, through any of them.",
    ),
    # R4 -- not target-aware.
    PolicyRestriction(
        code="owner_designation_cli_only",
        effect=RestrictionEffect.CLI_ONLY,
        permissions=frozenset({P.ORGANIZATION_OWNER_MANAGE}),
        affected_actors=frozenset({OrganizationRole.OWNER}),
        rule="Reserved Owner-only authority. Owner designation and transfer are CLI-only; holding this "
             "permission implies no web workflow.",
    ),
    # R5 -- not target-aware.
    PolicyRestriction(
        code="manager_discount_cap_equals_staff_cap",
        effect=RestrictionEffect.DISCOUNT_CAP,
        permissions=frozenset({P.DISCOUNTS_APPLY}),
        affected_actors=frozenset({BranchRole.MANAGER}),
        rule="A Branch Manager's discounts are capped at the staff discount cap, exactly as Branch Staff's are.",
    ),
)


# --- import-time validation ----------------------------------------------------------------------


def _policy_for(role) -> RolePolicy:
    if isinstance(role, OrganizationRole):
        return ORGANIZATION_ROLE_POLICY[role]
    return BRANCH_ROLE_POLICY[role]


def _validate() -> None:
    if set(ORGANIZATION_ROLE_POLICY) != set(OrganizationRole):
        raise PolicyDefinitionError("ORGANIZATION_ROLE_POLICY must cover exactly the OrganizationRole members")
    if set(BRANCH_ROLE_POLICY) != set(BranchRole):
        raise PolicyDefinitionError("BRANCH_ROLE_POLICY must cover exactly the BranchRole members")
    for policy in (*ORGANIZATION_ROLE_POLICY.values(), *BRANCH_ROLE_POLICY.values()):
        if not isinstance(policy, RolePolicy):
            raise PolicyDefinitionError("every policy must be a RolePolicy")
    if set(PERMISSION_SCOPES) != set(Permission):
        raise PolicyDefinitionError("every Permission needs a scope")
    for role, policy in BRANCH_ROLE_POLICY.items():
        org_scoped = [p for p in policy.allowed if PERMISSION_SCOPES[p] is not PermissionScope.BRANCH]
        if org_scoped:
            raise PolicyDefinitionError(
                f"branch role {role.value} is allowed organization-scoped permissions: {_names(org_scoped)}")
    codes = [r.code for r in POLICY_RESTRICTIONS]
    if len(codes) != len(set(codes)):
        raise PolicyDefinitionError("restriction codes must be unique")
    for restriction in POLICY_RESTRICTIONS:
        if not isinstance(restriction, PolicyRestriction):
            raise PolicyDefinitionError("every restriction must be a PolicyRestriction")
        if restriction.enforcement is not Enforcement.NOT_ENFORCED:
            raise PolicyDefinitionError(f"{restriction.code}: no restriction is enforced in O3.5")
        # An actor can only be limited in, or keep, a permission it holds.
        for role in restriction.affected_actors | restriction.permitted_actors:
            not_held = restriction.permissions - _policy_for(role).allowed
            if not_held:
                raise PolicyDefinitionError(
                    f"{restriction.code}: {role.value} is not allowed {_names(not_held)}, so cannot be an actor")
        if restriction.effect is RestrictionEffect.DISCOUNT_CAP:
            if restriction.permissions != frozenset({P.DISCOUNTS_APPLY}):
                raise PolicyDefinitionError(f"{restriction.code}: a discount cap concerns discounts.apply only")
            uncapped = [r.value for r in restriction.affected_actors
                        if _policy_for(r).discount_cap is not DiscountCap.STAFF_CAP]
            if uncapped:
                raise PolicyDefinitionError(
                    f"{restriction.code}: {sorted(uncapped)} do not carry the staff discount cap in their policy")


_validate()
