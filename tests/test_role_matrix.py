"""O3.5: the workforce permission policy (app/utils/role_matrix.py).

The policy states what each approved membership role -- ORGANIZATION_OWNER,
ORGANIZATION_ADMIN, BRANCH_MANAGER, BRANCH_STAFF -- is meant to hold. It is
**inert**: nothing in the application reads it, and these tests prove that
importing it changes no grant, map or authorization answer, and that the
O3.4 resolver still refuses stored Owner and Manager roles.

Every expected value below is written as a literal permission string, never
derived from the module under test, so a mistake in the module cannot hide
itself in its own expectation.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
import json
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from app.models.user import Role
from app.services.auth_context import WorkforceContextError, workforce_context
from app.utils import role_matrix as rm
from app.utils.permissions import (
    BRANCH_ROLE_GRANTS,
    COMPATIBILITY_GRANTS,
    ORGANIZATION_ROLE_GRANTS,
    PERMISSION_SCOPES,
    AuthContext,
    BranchRole,
    OrganizationRole,
    Permission,
    PermissionScope,
    has_permission,
    permissions_for,
)
from app.utils.role_matrix import (
    BRANCH_ROLE_POLICY,
    ORGANIZATION_ROLE_POLICY,
    POLICY_RESTRICTIONS,
    TARGET_AWARE_EFFECTS,
    TARGET_SENSITIVE_PERMISSIONS,
    DiscountCap,
    Enforcement,
    PolicyDefinitionError,
    PolicyRestriction,
    RestrictionCondition,
    RestrictionEffect,
    RolePolicy,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
P = Permission

# --- literal expectations ----------------------------------------------------------------------------

ORG_SCOPED = {
    "organization.view", "organization.owner.manage", "organization.admins.manage",
    "organization.members.manage", "menu.manage", "inventory.manage", "audit.view", "accounts.manage",
}
BRANCH_SCOPED = {
    "branch.members.manage", "branch.profile.manage", "tables.manage", "tables.operate", "orders.view",
    "orders.manage", "payments.record", "payments.view", "refunds.create", "discounts.apply",
    "settings.pricing.manage", "menu.view", "menu.availability", "inventory.view", "inventory.record",
    "inventory.adjust", "reservations.manage", "reports.view",
}
ALL_26 = ORG_SCOPED | BRANCH_SCOPED

STAFF_10 = {
    "tables.operate", "orders.view", "orders.manage", "payments.record", "discounts.apply",
    "reservations.manage", "menu.view", "menu.availability", "inventory.view", "inventory.record",
}
MANAGER_ADDITIONS = {"tables.manage", "payments.view", "reports.view", "inventory.adjust"}
MANAGER_DENIED_12 = {
    "refunds.create", "organization.view", "organization.owner.manage", "organization.admins.manage",
    "organization.members.manage", "accounts.manage", "menu.manage", "inventory.manage", "audit.view",
    "branch.members.manage", "branch.profile.manage", "settings.pricing.manage",
}
COMPAT_ADMIN_24 = ALL_26 - {"organization.view", "organization.owner.manage"}
TARGET_SENSITIVE_4 = {
    "accounts.manage", "organization.admins.manage", "organization.members.manage", "branch.members.manage",
}

EXPECTED = {
    OrganizationRole.OWNER: (ALL_26, set(), "admin_maximum"),
    OrganizationRole.ADMIN: (ALL_26 - {"organization.owner.manage"}, {"organization.owner.manage"}, "admin_maximum"),
    BranchRole.MANAGER: (STAFF_10 | MANAGER_ADDITIONS, MANAGER_DENIED_12, "staff_cap"),
    BranchRole.STAFF: (STAFF_10, ALL_26 - STAFF_10, "staff_cap"),
}
EXPECTED_COUNTS = {
    OrganizationRole.OWNER: (26, 0), OrganizationRole.ADMIN: (25, 1),
    BranchRole.MANAGER: (14, 12), BranchRole.STAFF: (10, 16),
}

_OWNER, _ADMIN, _MANAGER = OrganizationRole.OWNER, OrganizationRole.ADMIN, BranchRole.MANAGER

# R1-R5, field by field. ``affected`` are the actors the rule limits,
# ``permitted`` the actors that keep the authority, ``targets`` the roles of
# the account acted on.
EXPECTED_RESTRICTIONS = {
    "admin_cannot_act_on_owner": dict(
        effect="actor_denied", affected={_ADMIN}, permitted=set(), targets={_OWNER},
        condition="always", permissions=TARGET_SENSITIVE_4),
    "admins_manage_admins_only_without_owner": dict(
        effect="actor_allowed_only_while", affected={_ADMIN}, permitted=set(), targets={_ADMIN},
        condition="no_owner_exists", permissions=TARGET_SENSITIVE_4),
    "only_owner_manages_admins_when_owner_exists": dict(
        effect="exclusive_to_permitted_actors", affected={_ADMIN}, permitted={_OWNER}, targets={_ADMIN},
        condition="owner_exists", permissions=TARGET_SENSITIVE_4),
    "owner_designation_cli_only": dict(
        effect="cli_only", affected={_OWNER}, permitted=set(), targets=set(),
        condition="always", permissions={"organization.owner.manage"}),
    "manager_discount_cap_equals_staff_cap": dict(
        effect="discount_cap", affected={_MANAGER}, permitted=set(), targets=set(),
        condition="always", permissions={"discounts.apply"}),
}
R1, R2, R3, R4, R5 = EXPECTED_RESTRICTIONS


def restriction(code: str) -> PolicyRestriction:
    return next(r for r in POLICY_RESTRICTIONS if r.code == code)


ALL_ROLES = [*OrganizationRole, *BranchRole]


def policy(role) -> RolePolicy:
    return ORGANIZATION_ROLE_POLICY[role] if isinstance(role, OrganizationRole) else BRANCH_ROLE_POLICY[role]


def values(permissions) -> set[str]:
    return {p.value for p in permissions}


def ctx(role: Role, *, active: bool = True, approved: bool = True, **extra) -> AuthContext:
    return AuthContext(user_id=7, global_role=role, is_active=active, staff_approved=approved, **extra)


# ======================================================================================
# 1-2. Exact sets; all 26 classified for every role
# ======================================================================================

def test_the_catalog_still_has_exactly_the_26_reviewed_permissions():
    """A new permission must be classified for every role (the module refuses
    to import otherwise) and reviewed here."""
    assert len(Permission) == 26
    assert {p.value for p in Permission} == ALL_26


@pytest.mark.parametrize("role", ALL_ROLES, ids=lambda r: f"{type(r).__name__}.{r.name}")
def test_exact_allowed_and_denied_sets(role):
    allowed, denied, _ = EXPECTED[role]
    assert values(policy(role).allowed) == allowed
    assert values(policy(role).denied) == denied
    assert (len(policy(role).allowed), len(policy(role).denied)) == EXPECTED_COUNTS[role]


@pytest.mark.parametrize("role", ALL_ROLES, ids=lambda r: f"{type(r).__name__}.{r.name}")
def test_every_permission_is_classified_exactly_once(role):
    p = policy(role)
    assert not (p.allowed & p.denied)
    assert p.allowed | p.denied == frozenset(Permission)
    for permission in Permission:
        assert (permission in p.allowed) != (permission in p.denied), permission


def test_policy_maps_cover_exactly_the_membership_roles():
    assert set(ORGANIZATION_ROLE_POLICY) == {OrganizationRole.OWNER, OrganizationRole.ADMIN}
    assert set(BRANCH_ROLE_POLICY) == {BranchRole.MANAGER, BranchRole.STAFF}


def test_roles_form_a_strict_hierarchy():
    staff, manager = BRANCH_ROLE_POLICY[BranchRole.STAFF], BRANCH_ROLE_POLICY[BranchRole.MANAGER]
    admin, owner = ORGANIZATION_ROLE_POLICY[OrganizationRole.ADMIN], ORGANIZATION_ROLE_POLICY[OrganizationRole.OWNER]
    assert staff.allowed < manager.allowed < admin.allowed < owner.allowed


# ======================================================================================
# 3. No unknown identifiers; construction is checked
# ======================================================================================

def test_policies_and_restrictions_hold_only_permission_members():
    for role in ALL_ROLES:
        assert all(type(p) is Permission for p in policy(role).allowed | policy(role).denied)
    for r in POLICY_RESTRICTIONS:
        assert all(type(p) is Permission for p in r.permissions)
    assert all(type(p) is Permission for p in TARGET_SENSITIVE_PERMISSIONS)


def _fields(**overrides):
    fields = dict(allowed=frozenset(Permission), denied=frozenset(), discount_cap=DiscountCap.STAFF_CAP)
    fields.update(overrides)
    return fields


@pytest.mark.parametrize("overrides", [
    dict(allowed=frozenset(Permission) - {P.REFUNDS_CREATE} | {"refunds.create"}),   # raw string
    dict(allowed=frozenset(Permission) | {"reports.export"}),                         # unknown identifier
    dict(allowed=set(Permission)),                                                    # mutable set
    dict(allowed=frozenset(Permission) - {P.AUDIT_VIEW}),                             # unclassified
    dict(denied=frozenset({P.AUDIT_VIEW})),                                           # in both
    dict(discount_cap="staff_cap"),                                                   # loose cap
], ids=["raw-string", "unknown", "mutable-set", "unclassified", "overlap", "loose-cap"])
def test_a_malformed_role_policy_is_refused(overrides):
    with pytest.raises(PolicyDefinitionError):
        RolePolicy(**_fields(**overrides))


def _restriction(**overrides):
    """A well-formed target-aware restriction (R3's shape), then overridden."""
    fields = dict(code="x", effect=RestrictionEffect.EXCLUSIVE_TO_PERMITTED_ACTORS,
                  permissions=TARGET_SENSITIVE_PERMISSIONS, affected_actors=frozenset({_ADMIN}),
                  permitted_actors=frozenset({_OWNER}), protected_targets=frozenset({_ADMIN}),
                  condition=RestrictionCondition.OWNER_EXISTS, rule="r")
    fields.update(overrides)
    return fields


def _plain(**overrides):
    """A well-formed restriction that is not target-aware (R4's shape)."""
    fields = dict(code="x", effect=RestrictionEffect.CLI_ONLY, permissions=frozenset({P.ORGANIZATION_OWNER_MANAGE}),
                  affected_actors=frozenset({_OWNER}), rule="r")
    fields.update(overrides)
    return fields


def test_the_well_formed_restriction_shapes_are_accepted():
    PolicyRestriction(**_restriction())
    PolicyRestriction(**_plain())


@pytest.mark.parametrize("overrides", [
    dict(code=""), dict(rule=""),
    dict(effect="exclusive_to_permitted_actors"), dict(condition="owner_exists"),
    dict(affected_actors=frozenset()), dict(affected_actors=frozenset({Role.ADMIN})),
    dict(affected_actors=frozenset({"customer"})), dict(affected_actors={_ADMIN}),
    dict(protected_targets=frozenset({Role.ADMIN})), dict(protected_targets={_ADMIN}),
    dict(permitted_actors=frozenset({Role.ADMIN})), dict(permitted_actors={_OWNER}),
    dict(permissions=frozenset()), dict(permissions=frozenset(TARGET_SENSITIVE_4)),
    dict(enforcement="enforced"), dict(enforcement=True),
], ids=["no-code", "no-rule", "loose-effect", "loose-condition", "no-actors", "global-role-actor",
        "customer-actor", "mutable-actors", "global-role-target", "mutable-targets", "global-role-permitted",
        "mutable-permitted", "no-permissions", "raw-permissions", "enforced-string", "enforced-bool"])
def test_a_malformed_restriction_is_refused(overrides):
    with pytest.raises(PolicyDefinitionError):
        PolicyRestriction(**_restriction(**overrides))


@pytest.mark.parametrize("permission", sorted(TARGET_SENSITIVE_4))
@pytest.mark.parametrize("effect", sorted(TARGET_AWARE_EFFECTS, key=lambda e: e.value), ids=lambda e: e.value)
def test_a_target_aware_restriction_missing_one_of_the_four_is_refused(effect, permission):
    """The defect this corrects: a restriction about a target that named
    fewer than the four target-sensitive permissions could be bypassed
    through the others, so it cannot even be built."""
    fields = _restriction(effect=effect, permissions=TARGET_SENSITIVE_PERMISSIONS - {Permission(permission)})
    if effect is not RestrictionEffect.EXCLUSIVE_TO_PERMITTED_ACTORS:
        fields.update(permitted_actors=frozenset())
    with pytest.raises(PolicyDefinitionError, match="every target-sensitive permission"):
        PolicyRestriction(**fields)


def test_a_target_aware_restriction_on_one_permission_is_refused():
    with pytest.raises(PolicyDefinitionError, match="every target-sensitive permission"):
        PolicyRestriction(**_restriction(permissions=frozenset({P.ORGANIZATION_ADMINS_MANAGE})))


@pytest.mark.parametrize("fields", [
    _restriction(protected_targets=frozenset()),                                    # no target named
    _restriction(permitted_actors=frozenset()),                                     # exclusive, nobody permitted
    _restriction(permitted_actors=frozenset({_ADMIN})),                             # permitted == affected
    _restriction(affected_actors=frozenset({_OWNER, _ADMIN})),                      # Owner both permitted and affected
    _restriction(effect=RestrictionEffect.ACTOR_DENIED),                            # denial naming permitted actors
    _restriction(effect=RestrictionEffect.ACTOR_ALLOWED_ONLY_WHILE),                # allowance naming permitted actors
    _restriction(effect=RestrictionEffect.ACTOR_ALLOWED_ONLY_WHILE, permitted_actors=frozenset(),
                 condition=RestrictionCondition.ALWAYS),                            # "only while" with no condition
    _plain(protected_targets=frozenset({_ADMIN})),                                  # CLI-only with a target
    _plain(permitted_actors=frozenset({_OWNER})),                                   # CLI-only with permitted actors
    _plain(condition=RestrictionCondition.OWNER_EXISTS),                            # CLI-only with a condition
    _plain(effect=RestrictionEffect.DISCOUNT_CAP, protected_targets=frozenset({_ADMIN})),
], ids=["no-target", "exclusive-nobody-permitted", "permitted-equals-affected", "owner-permitted-and-affected",
        "denied-with-permitted", "only-while-with-permitted", "only-while-always", "cli-with-target",
        "cli-with-permitted", "cli-with-condition", "cap-with-target"])
def test_an_ambiguous_restriction_shape_is_refused(fields):
    with pytest.raises(PolicyDefinitionError):
        PolicyRestriction(**fields)


# ======================================================================================
# 4-5. Scope
# ======================================================================================

def test_scope_classification_is_the_reviewed_8_and_18():
    assert {p.value for p, s in PERMISSION_SCOPES.items() if s is PermissionScope.ORGANIZATION} == ORG_SCOPED
    assert {p.value for p, s in PERMISSION_SCOPES.items() if s is PermissionScope.BRANCH} == BRANCH_SCOPED


@pytest.mark.parametrize("role", list(BranchRole), ids=lambda r: r.name)
def test_branch_roles_never_receive_organization_scoped_permissions(role):
    assert not (values(policy(role).allowed) & ORG_SCOPED)
    assert all(PERMISSION_SCOPES[p] is PermissionScope.BRANCH for p in policy(role).allowed)
    assert ORG_SCOPED <= values(policy(role).denied)


@pytest.mark.parametrize("role", list(OrganizationRole), ids=lambda r: r.name)
def test_organization_roles_hold_every_branch_permission(role):
    assert BRANCH_SCOPED <= values(policy(role).allowed)


# ======================================================================================
# 6-7. Approved denials; Owner-only authority
# ======================================================================================

@pytest.mark.parametrize("permission", sorted(MANAGER_DENIED_12))
def test_branch_manager_is_denied(permission):
    assert Permission(permission) in BRANCH_ROLE_POLICY[BranchRole.MANAGER].denied
    assert Permission(permission) not in BRANCH_ROLE_POLICY[BranchRole.MANAGER].allowed


@pytest.mark.parametrize("permission", sorted(MANAGER_ADDITIONS | {"discounts.apply"}))
def test_branch_manager_is_allowed_its_approved_additions(permission):
    assert Permission(permission) in BRANCH_ROLE_POLICY[BranchRole.MANAGER].allowed


def test_branch_manager_cannot_refund_invite_or_edit_the_branch_profile():
    manager = BRANCH_ROLE_POLICY[BranchRole.MANAGER]
    assert {P.REFUNDS_CREATE, P.ORGANIZATION_MEMBERS_MANAGE, P.BRANCH_MEMBERS_MANAGE,
            P.BRANCH_PROFILE_MANAGE} <= manager.denied
    assert P.TABLES_MANAGE in manager.allowed   # physical tables, but not the customer-facing profile


def test_branch_staff_hold_no_management_permission():
    assert values(BRANCH_ROLE_POLICY[BranchRole.STAFF].allowed) == STAFF_10


def test_organization_owner_manage_is_owner_only():
    holders = [role for role in ALL_ROLES if P.ORGANIZATION_OWNER_MANAGE in policy(role).allowed]
    assert holders == [OrganizationRole.OWNER]
    assert P.ORGANIZATION_OWNER_MANAGE in ORGANIZATION_ROLE_POLICY[OrganizationRole.ADMIN].denied


def test_owner_holds_all_26_including_refunds():
    owner = ORGANIZATION_ROLE_POLICY[OrganizationRole.OWNER]
    assert owner.allowed == frozenset(Permission) and owner.denied == frozenset()
    assert P.REFUNDS_CREATE in owner.allowed


# ======================================================================================
# 8. Target-aware restrictions: recorded, not enforced
# ======================================================================================

def test_restrictions_are_exactly_r1_to_r5():
    assert [r.code for r in POLICY_RESTRICTIONS] == list(EXPECTED_RESTRICTIONS)


@pytest.mark.parametrize("code", list(EXPECTED_RESTRICTIONS))
def test_restriction_states_its_actors_targets_condition_and_permissions(code):
    expected, actual = EXPECTED_RESTRICTIONS[code], restriction(code)
    assert actual.effect.value == expected["effect"]
    assert set(actual.affected_actors) == expected["affected"]
    assert set(actual.permitted_actors) == expected["permitted"]
    assert set(actual.protected_targets) == expected["targets"]
    assert actual.condition.value == expected["condition"]
    assert values(actual.permissions) == expected["permissions"]
    assert actual.enforcement is Enforcement.NOT_ENFORCED
    assert actual.rule
    # Members of the real role enums, not look-alike strings.
    for role in actual.affected_actors | actual.permitted_actors | actual.protected_targets:
        assert type(role) in (OrganizationRole, BranchRole)


def test_target_sensitive_permissions_are_the_approved_four():
    assert values(TARGET_SENSITIVE_PERMISSIONS) == TARGET_SENSITIVE_4


def test_the_effect_and_condition_vocabularies_are_the_reviewed_ones():
    assert {e.value for e in RestrictionEffect} == {
        "actor_denied", "actor_allowed_only_while", "exclusive_to_permitted_actors", "cli_only", "discount_cap"}
    assert {e.value for e in TARGET_AWARE_EFFECTS} == {
        "actor_denied", "actor_allowed_only_while", "exclusive_to_permitted_actors"}
    assert {c.value for c in RestrictionCondition} == {"always", "no_owner_exists", "owner_exists"}


def test_no_restriction_is_marked_enforced():
    assert [e.value for e in Enforcement] == ["not_enforced"]
    assert all(r.enforcement is Enforcement.NOT_ENFORCED for r in POLICY_RESTRICTIONS)


def test_every_actor_holds_the_permissions_its_restriction_names():
    for r in POLICY_RESTRICTIONS:
        for role in r.affected_actors | r.permitted_actors:
            assert r.permissions <= policy(role).allowed, r.code


@pytest.mark.parametrize("code", [R1, R2, R3])
def test_every_target_aware_restriction_covers_all_four_permissions(code):
    """R1, R2 and R3 alike: none can be bypassed through a permission it
    leaves out."""
    r = restriction(code)
    assert r.effect in TARGET_AWARE_EFFECTS
    assert values(r.permissions) == TARGET_SENSITIVE_4
    assert r.permissions == TARGET_SENSITIVE_PERMISSIONS
    assert r.protected_targets


def test_the_target_aware_restrictions_are_exactly_r1_r2_r3():
    assert [r.code for r in POLICY_RESTRICTIONS if r.effect in TARGET_AWARE_EFFECTS] == [R1, R2, R3]
    assert [r.code for r in POLICY_RESTRICTIONS if r.protected_targets] == [R1, R2, R3]


# --- R3 in particular -------------------------------------------------------------------------------

@pytest.mark.parametrize("permission", sorted(TARGET_SENSITIVE_4))
def test_r3_covers_each_target_sensitive_permission(permission):
    assert Permission(permission) in restriction(R3).permissions


def test_r3_names_the_admin_actor_the_owner_and_the_admin_target_once_an_owner_exists():
    r3 = restriction(R3)
    assert r3.effect is RestrictionEffect.EXCLUSIVE_TO_PERMITTED_ACTORS
    assert r3.affected_actors == {OrganizationRole.ADMIN}      # the actor it limits
    assert r3.permitted_actors == {OrganizationRole.OWNER}     # the only actor left with the authority
    assert r3.protected_targets == {OrganizationRole.ADMIN}    # the account acted on
    assert r3.condition is RestrictionCondition.OWNER_EXISTS
    assert values(r3.permissions) == TARGET_SENSITIVE_4
    assert r3.enforcement is Enforcement.NOT_ENFORCED


def test_r3_is_not_merely_attached_to_the_owner():
    """The Owner is the permitted actor, never the limited one; the Admin is
    limited, never permitted."""
    r3 = restriction(R3)
    assert OrganizationRole.OWNER not in r3.affected_actors
    assert OrganizationRole.ADMIN not in r3.permitted_actors
    assert not (r3.affected_actors & r3.permitted_actors)
    assert not any(isinstance(role, BranchRole)
                   for role in r3.affected_actors | r3.permitted_actors | r3.protected_targets)


def test_r2_and_r3_are_the_two_sides_of_one_rule():
    """Same actor, same target, same four permissions; R2 allows while no
    Owner exists, R3 withdraws it once one does."""
    r2, r3 = restriction(R2), restriction(R3)
    assert r2.affected_actors == r3.affected_actors == {OrganizationRole.ADMIN}
    assert r2.protected_targets == r3.protected_targets == {OrganizationRole.ADMIN}
    assert r2.permissions == r3.permissions == TARGET_SENSITIVE_PERMISSIONS
    assert (r2.effect, r2.condition) == (RestrictionEffect.ACTOR_ALLOWED_ONLY_WHILE,
                                         RestrictionCondition.NO_OWNER_EXISTS)
    assert (r3.effect, r3.condition) == (RestrictionEffect.EXCLUSIVE_TO_PERMITTED_ACTORS,
                                         RestrictionCondition.OWNER_EXISTS)


def test_r1_protects_the_owner_from_admins_unconditionally():
    r1 = restriction(R1)
    assert (r1.effect, r1.condition) == (RestrictionEffect.ACTOR_DENIED, RestrictionCondition.ALWAYS)
    assert r1.affected_actors == {OrganizationRole.ADMIN} and r1.protected_targets == {OrganizationRole.OWNER}
    assert r1.permitted_actors == frozenset()


def test_no_restriction_limits_the_owner_against_a_target():
    """R1-R3 limit the Admin only; the Owner's reserved authority is limited
    by R4 alone (CLI-only), which names no target."""
    for r in POLICY_RESTRICTIONS:
        if r.effect in TARGET_AWARE_EFFECTS:
            assert r.affected_actors == {OrganizationRole.ADMIN}, r.code
    assert restriction(R4).affected_actors == {OrganizationRole.OWNER}
    assert restriction(R4).protected_targets == frozenset()


@pytest.mark.parametrize("permission", sorted(TARGET_SENSITIVE_4))
@pytest.mark.parametrize("organization_role", [None, OrganizationRole.ADMIN])
def test_restrictions_do_not_limit_todays_admin(permission, organization_role):
    """R1-R3 are metadata: an ADMIN context still holds every target-sensitive
    permission unconditionally, exactly as before O3.5. The evaluation takes
    no target and no organization state, so no restriction can be acting."""
    context = ctx(Role.ADMIN, organization_role=organization_role, organization_id=1, branch_id=1)
    assert has_permission(context, Permission(permission))
    assert Permission(permission) in COMPATIBILITY_GRANTS[Role.ADMIN]


@pytest.mark.parametrize("permission", sorted(TARGET_SENSITIVE_4))
def test_a_context_carrying_the_owner_role_gains_nothing_from_r3(permission):
    """R3 names the Owner as the permitted actor. That grants nothing at
    runtime: an Owner-role context holds a target-sensitive permission only
    if its global role's compatibility grants already do."""
    for role in Role:
        context = ctx(role, organization_role=OrganizationRole.OWNER, organization_id=1, branch_id=1)
        assert has_permission(context, Permission(permission)) == (Permission(permission) in COMPATIBILITY_GRANTS[role])


# ======================================================================================
# 9. Discount caps
# ======================================================================================

@pytest.mark.parametrize("role", ALL_ROLES, ids=lambda r: f"{type(r).__name__}.{r.name}")
def test_discount_caps(role):
    assert policy(role).discount_cap.value == EXPECTED[role][2]


def test_manager_cap_restriction_matches_the_staff_cap():
    r5 = restriction(R5)
    assert r5.effect is RestrictionEffect.DISCOUNT_CAP
    assert r5.affected_actors == {BranchRole.MANAGER}
    assert BRANCH_ROLE_POLICY[BranchRole.MANAGER].discount_cap is DiscountCap.STAFF_CAP
    assert BRANCH_ROLE_POLICY[BranchRole.MANAGER].discount_cap is BRANCH_ROLE_POLICY[BranchRole.STAFF].discount_cap
    assert {c.value for c in DiscountCap} == {"admin_maximum", "staff_cap"}


# ======================================================================================
# 10. Immutability
# ======================================================================================

def test_policy_maps_are_read_only():
    for mapping in (ORGANIZATION_ROLE_POLICY, BRANCH_ROLE_POLICY):
        assert isinstance(mapping, MappingProxyType)
        with pytest.raises(TypeError):
            mapping[next(iter(mapping))] = None
        with pytest.raises(TypeError):
            del mapping[next(iter(mapping))]


def test_policy_objects_are_frozen():
    manager = BRANCH_ROLE_POLICY[BranchRole.MANAGER]
    with pytest.raises(dataclasses.FrozenInstanceError):
        manager.allowed = frozenset(Permission)
    with pytest.raises(dataclasses.FrozenInstanceError):
        manager.discount_cap = DiscountCap.ADMIN_MAXIMUM
    with pytest.raises(AttributeError):
        manager.allowed.add(P.REFUNDS_CREATE)
    with pytest.raises((AttributeError, TypeError)):   # slots: no new attributes
        object.__setattr__(manager, "extra", 1)
    assert type(TARGET_SENSITIVE_PERMISSIONS) is frozenset and type(TARGET_AWARE_EFFECTS) is frozenset
    for role in ALL_ROLES:
        assert type(policy(role).allowed) is frozenset and type(policy(role).denied) is frozenset


_RESTRICTION_FIELDS = ["code", "effect", "permissions", "affected_actors", "rule", "protected_targets",
                       "permitted_actors", "condition", "enforcement"]


def test_restriction_fields_are_the_reviewed_ones():
    assert [f.name for f in dataclasses.fields(PolicyRestriction)] == _RESTRICTION_FIELDS


@pytest.mark.parametrize("code", list(EXPECTED_RESTRICTIONS))
def test_restrictions_are_frozen(code):
    assert isinstance(POLICY_RESTRICTIONS, tuple)
    r = restriction(code)
    for name in _RESTRICTION_FIELDS:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(r, name, getattr(r, name))
    with pytest.raises((AttributeError, TypeError)):   # slots: no new attributes
        object.__setattr__(r, "extra", 1)
    for name in ("permissions", "affected_actors", "protected_targets", "permitted_actors"):
        value = getattr(r, name)
        assert type(value) is frozenset
        with pytest.raises(AttributeError):
            value.add(OrganizationRole.OWNER)
    assert hash(r) == hash(restriction(code))


def test_r3_cannot_be_narrowed_or_reassigned_in_place():
    r3 = restriction(R3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r3.permissions = frozenset({P.ORGANIZATION_ADMINS_MANAGE})
    with pytest.raises(dataclasses.FrozenInstanceError):
        r3.affected_actors = frozenset({OrganizationRole.OWNER})
    with pytest.raises(dataclasses.FrozenInstanceError):
        r3.enforcement = "enforced"
    with pytest.raises(TypeError):
        POLICY_RESTRICTIONS[2] = None
    # A narrowed copy cannot be built either.
    with pytest.raises(PolicyDefinitionError):
        dataclasses.replace(r3, permissions=frozenset({P.ORGANIZATION_ADMINS_MANAGE}))


# ======================================================================================
# 11. No CUSTOMER workforce role
# ======================================================================================

def test_no_customer_role_in_the_policy():
    keys = [*ORGANIZATION_ROLE_POLICY, *BRANCH_ROLE_POLICY]
    assert not any(isinstance(k, Role) for k in keys)
    in_restrictions = [role for x in POLICY_RESTRICTIONS
                       for role in x.affected_actors | x.permitted_actors | x.protected_targets]
    named = {k.value for k in keys} | {role.value for role in in_restrictions}
    assert "customer" not in named and Role.CUSTOMER.value not in named
    assert not any(isinstance(role, Role) for role in in_restrictions)


# ======================================================================================
# 12. Compared with the compatibility grants
# ======================================================================================

def test_compatibility_grants_are_the_reviewed_baseline():
    assert values(COMPATIBILITY_GRANTS[Role.STAFF]) == STAFF_10
    assert values(COMPATIBILITY_GRANTS[Role.ADMIN]) == COMPAT_ADMIN_24
    assert COMPATIBILITY_GRANTS[Role.CUSTOMER] == frozenset()


def test_intentional_differences_from_compatibility():
    compat_staff, compat_admin = COMPATIBILITY_GRANTS[Role.STAFF], COMPATIBILITY_GRANTS[Role.ADMIN]
    staff = BRANCH_ROLE_POLICY[BranchRole.STAFF].allowed
    manager = BRANCH_ROLE_POLICY[BranchRole.MANAGER].allowed
    admin = ORGANIZATION_ROLE_POLICY[OrganizationRole.ADMIN].allowed
    owner = ORGANIZATION_ROLE_POLICY[OrganizationRole.OWNER].allowed

    assert staff == compat_staff                                         # no difference
    assert values(manager - compat_staff) == MANAGER_ADDITIONS           # +4 over STAFF
    assert manager < compat_admin                                        # strictly less than ADMIN
    assert values(admin - compat_admin) == {"organization.view"}         # +1
    assert values(owner - compat_admin) == {"organization.view", "organization.owner.manage"}   # +2


def test_no_policy_removes_a_permission_its_global_role_holds_today():
    assert COMPATIBILITY_GRANTS[Role.STAFF] <= BRANCH_ROLE_POLICY[BranchRole.STAFF].allowed
    assert COMPATIBILITY_GRANTS[Role.ADMIN] <= ORGANIZATION_ROLE_POLICY[OrganizationRole.ADMIN].allowed
    assert COMPATIBILITY_GRANTS[Role.ADMIN] <= ORGANIZATION_ROLE_POLICY[OrganizationRole.OWNER].allowed


# ======================================================================================
# 13-14. Inertness: nothing reads the policy; runtime grants unchanged
# ======================================================================================

_PRODUCTION_SOURCES = ["app", "scripts", "migrations", "run.py", "config.py"]


def _production_python_files():
    for entry in _PRODUCTION_SOURCES:
        path = REPO_ROOT / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.py"))


def test_no_production_module_imports_or_names_the_policy():
    own = REPO_ROOT / "app" / "utils" / "role_matrix.py"
    offenders = []
    for path in _production_python_files():
        if path == own:
            continue
        source = path.read_text(encoding="utf-8")
        if "role_matrix" in source:
            offenders.append(str(path.relative_to(REPO_ROOT)))
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module and "role_matrix" in node.module:
                offenders.append(str(path.relative_to(REPO_ROOT)))
            elif isinstance(node, ast.Import) and any("role_matrix" in a.name for a in node.names):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_the_policy_module_uses_explicit_raises_not_asserts():
    tree = ast.parse((REPO_ROOT / "app" / "utils" / "role_matrix.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))


_SUBPROCESS_PROBE = r"""
import itertools, json, sys
sys.path.insert(0, sys.argv[1])
import importlib, pkgutil
import app as app_package
from app import create_app
create_app("testing")                       # the application as it starts
# ...and every other module under app/, including ones nothing imports yet
# (such as app.services.auth_context): none may load the policy.
for module in pkgutil.walk_packages(app_package.__path__, "app."):
    if module.name != "app.utils.role_matrix":
        importlib.import_module(module.name)
loaded_by_app = "app.utils.role_matrix" in sys.modules
import app.utils.permissions as p
from app.models.user import Role

def snapshot():
    contexts = []
    for role, org_role, branch_role, active, approved in itertools.product(
            list(Role), [None, *p.OrganizationRole], [None, *p.BranchRole], [True, False], [True, False]):
        c = p.AuthContext(user_id=7, global_role=role, is_active=active, staff_approved=approved,
                          organization_id=1, branch_id=1, organization_role=org_role, branch_role=branch_role)
        contexts.append(sorted(x.value for x in p.permissions_for(c)))
    def table(m):
        return sorted((k.value, sorted(x.value for x in v)) for k, v in m.items())
    return {
        "compat": table(p.COMPATIBILITY_GRANTS),
        "org_role_grants": table(p.ORGANIZATION_ROLE_GRANTS),
        "branch_role_grants": table(p.BRANCH_ROLE_GRANTS),
        "scopes": sorted((k.value, v.value) for k, v in p.PERMISSION_SCOPES.items()),
        "permissions_for": contexts,
    }

before = snapshot()
import app.utils.role_matrix
after = snapshot()
print(json.dumps({"loaded_by_app": loaded_by_app, "unchanged": before == after,
                  "contexts": len(before["permissions_for"])}))
"""


def test_importing_the_policy_changes_nothing_in_a_fresh_interpreter():
    """A fresh interpreter, so no earlier test import can mask an effect:
    building the whole application does not load the policy, and loading it
    afterwards changes no grant map, scope or ``permissions_for`` answer."""
    result = subprocess.run([sys.executable, "-c", _SUBPROCESS_PROBE, str(REPO_ROOT)],
                            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"loaded_by_app": False, "unchanged": True, "contexts": 3 * 3 * 3 * 2 * 2}


def test_runtime_role_grant_maps_are_still_empty():
    assert set(ORGANIZATION_ROLE_GRANTS) == set(OrganizationRole)
    assert set(BRANCH_ROLE_GRANTS) == set(BranchRole)
    assert all(grants == frozenset() for grants in ORGANIZATION_ROLE_GRANTS.values())
    assert all(grants == frozenset() for grants in BRANCH_ROLE_GRANTS.values())


# Every runtime context shape: global role (3) x organization role (None,
# owner, admin) x branch role (None, manager, staff) x active (2) x
# staff_approved (2) = 108 cases, each checked against its exact expected
# permission set. The same 108 combinations as the fresh-interpreter probe
# above, which proves they are *unchanged* by importing the policy; this test
# proves each one is *correct*.
_EXPECTED_GLOBAL_GRANTS = {Role.ADMIN: COMPAT_ADMIN_24, Role.STAFF: STAFF_10, Role.CUSTOMER: set()}


def _expected_permissions(role: Role, active: bool, approved: bool) -> set[str]:
    """Written out from the approved eligibility rules, independently of
    ``AuthContext.is_eligible``:

    - an inactive account holds nothing;
    - a customer holds nothing;
    - an active STAFF account awaiting approval holds nothing (pending
      approval applies to STAFF only);
    - an active ADMIN holds the ADMIN compatibility grants whatever
      ``staff_approved`` says;
    - otherwise the global role's compatibility grants.

    Membership roles never change the answer: the policy is inert and the
    runtime role grant maps are empty."""
    if not active:
        return set()
    if role is Role.CUSTOMER:
        return set()
    if role is Role.STAFF and not approved:
        return set()
    return _EXPECTED_GLOBAL_GRANTS[role]


def _context_case_id(role, org_role, branch_role, active, approved) -> str:
    return "-".join([
        role.value,
        f"org_{org_role.value if org_role else 'none'}",
        f"branch_{branch_role.value if branch_role else 'none'}",
        "active" if active else "inactive",
        "approved" if approved else "unapproved",
    ])


_CONTEXT_CASES = [
    pytest.param(*combo, id=_context_case_id(*combo))
    for combo in itertools.product(
        list(Role), [None, *OrganizationRole], [None, *BranchRole], [True, False], [True, False])
]


def test_the_context_matrix_has_108_distinct_cases():
    assert len(_CONTEXT_CASES) == 3 * 3 * 3 * 2 * 2 == 108
    assert len({case.id for case in _CONTEXT_CASES}) == 108


@pytest.mark.parametrize("role, org_role, branch_role, active, approved", _CONTEXT_CASES)
def test_permissions_for_ignores_the_policy(role, org_role, branch_role, active, approved):
    """With the policy imported, every context -- whatever membership roles it
    carries -- holds exactly the permissions the approved eligibility rules
    give its global role, never the policy's."""
    context = ctx(role, active=active, approved=approved, organization_role=org_role, branch_role=branch_role,
                  organization_id=1, branch_id=1)
    expected = _expected_permissions(role, active, approved)
    assert values(permissions_for(context)) == expected
    if expected:
        assert permissions_for(context) == COMPATIBILITY_GRANTS[role]
    else:
        assert permissions_for(context) == frozenset()
    assert not has_permission(context, P.ORGANIZATION_OWNER_MANAGE)
    assert not has_permission(context, P.ORGANIZATION_VIEW)


def test_a_branch_manager_context_holds_none_of_the_manager_additions():
    context = ctx(Role.STAFF, branch_role=BranchRole.MANAGER, organization_id=1, branch_id=1)
    assert not any(has_permission(context, Permission(p)) for p in MANAGER_ADDITIONS)


# ======================================================================================
# Import-time validation rejects a broken policy
# ======================================================================================

def _manager_with(**changes):
    manager = BRANCH_ROLE_POLICY[BranchRole.MANAGER]
    return dataclasses.replace(manager, **changes)


def test_validation_accepts_the_shipped_policy():
    rm._validate()


def test_validation_rejects_an_organization_permission_for_a_branch_role(monkeypatch):
    manager = BRANCH_ROLE_POLICY[BranchRole.MANAGER]
    bad = _manager_with(allowed=manager.allowed | {P.MENU_MANAGE}, denied=manager.denied - {P.MENU_MANAGE})
    monkeypatch.setattr(rm, "BRANCH_ROLE_POLICY",
                        MappingProxyType({BranchRole.MANAGER: bad, BranchRole.STAFF: BRANCH_ROLE_POLICY[BranchRole.STAFF]}))
    with pytest.raises(PolicyDefinitionError, match="organization-scoped"):
        rm._validate()


def test_validation_rejects_a_missing_role(monkeypatch):
    monkeypatch.setattr(rm, "ORGANIZATION_ROLE_POLICY",
                        MappingProxyType({OrganizationRole.ADMIN: ORGANIZATION_ROLE_POLICY[OrganizationRole.ADMIN]}))
    with pytest.raises(PolicyDefinitionError, match="ORGANIZATION_ROLE_POLICY"):
        rm._validate()


def test_validation_rejects_a_restriction_on_a_permission_the_role_lacks(monkeypatch):
    stray = PolicyRestriction(code="stray", effect=RestrictionEffect.CLI_ONLY,
                              affected_actors=frozenset({BranchRole.MANAGER}),
                              permissions=frozenset({P.REFUNDS_CREATE}), rule="r")
    monkeypatch.setattr(rm, "POLICY_RESTRICTIONS", (*POLICY_RESTRICTIONS, stray))
    with pytest.raises(PolicyDefinitionError, match="stray"):
        rm._validate()


@pytest.mark.parametrize("fields", [
    # A branch role cannot be the limited actor of a target-aware rule: it holds none of the four.
    dict(affected_actors=frozenset({BranchRole.MANAGER})),
    # ...nor the permitted one.
    dict(permitted_actors=frozenset({BranchRole.MANAGER})),
], ids=["affected-lacks-the-four", "permitted-lacks-the-four"])
def test_validation_rejects_a_target_aware_actor_that_lacks_the_permissions(monkeypatch, fields):
    stray = PolicyRestriction(**_restriction(code="stray", **fields))
    monkeypatch.setattr(rm, "POLICY_RESTRICTIONS", (*POLICY_RESTRICTIONS, stray))
    with pytest.raises(PolicyDefinitionError, match="stray"):
        rm._validate()


@pytest.mark.parametrize("fields", [
    # Organization Admin carries the ADMIN maximum, not the staff cap.
    dict(affected_actors=frozenset({OrganizationRole.ADMIN}), permissions=frozenset({P.DISCOUNTS_APPLY})),
    # A cap is about discounts.apply and nothing else.
    dict(affected_actors=frozenset({BranchRole.MANAGER}), permissions=frozenset({P.DISCOUNTS_APPLY, P.ORDERS_MANAGE})),
], ids=["uncapped-role", "other-permission"])
def test_validation_rejects_an_inconsistent_discount_cap_restriction(monkeypatch, fields):
    stray = PolicyRestriction(code="stray", effect=RestrictionEffect.DISCOUNT_CAP, rule="r", **fields)
    monkeypatch.setattr(rm, "POLICY_RESTRICTIONS", (*POLICY_RESTRICTIONS, stray))
    with pytest.raises(PolicyDefinitionError, match="stray"):
        rm._validate()


def test_validation_rejects_duplicate_restriction_codes(monkeypatch):
    monkeypatch.setattr(rm, "POLICY_RESTRICTIONS", (*POLICY_RESTRICTIONS, POLICY_RESTRICTIONS[0]))
    with pytest.raises(PolicyDefinitionError, match="unique"):
        rm._validate()


# ======================================================================================
# 15. The O3.4 resolver still fails closed for Owner and Manager
# ======================================================================================

def _row(organization_role=None, branch_role="staff"):
    return SimpleNamespace(membership_id=10, membership_organization_id=3, membership_status="active",
                           organization_role=organization_role, assignment_id=20, assignment_organization_id=3,
                           branch_id=2, assignment_status="active", branch_role=branch_role)


@pytest.mark.parametrize("role, organization_role, branch_role, code", [
    (Role.ADMIN, "owner", None, "unexpected_owner"),
    (Role.STAFF, "owner", "staff", "unexpected_owner"),
    (Role.ADMIN, "owner", "manager", "unexpected_owner"),
    (Role.STAFF, None, "manager", "role_mismatch"),
    (Role.ADMIN, "admin", "manager", "role_mismatch"),
], ids=["admin-owner", "staff-owner", "admin-owner-manager", "staff-manager", "admin-manager"])
def test_resolver_still_rejects_stored_owner_and_manager(role, organization_role, branch_role, code):
    with pytest.raises(WorkforceContextError) as caught:
        workforce_context(ctx(role), 3, 2, [_row(organization_role, branch_role)])
    assert caught.value.code == code


@pytest.mark.parametrize("role, organization_role, branch_role", [
    (Role.ADMIN, "admin", None), (Role.STAFF, None, "staff"),
])
def test_resolver_still_accepts_the_o33_roles_and_they_add_nothing(role, organization_role, branch_role):
    context = workforce_context(ctx(role), 3, 2, [_row(organization_role, branch_role)])
    assert permissions_for(context) == COMPATIBILITY_GRANTS[role]
