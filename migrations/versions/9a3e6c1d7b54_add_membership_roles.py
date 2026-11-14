"""add organization and branch membership roles

Revision ID: 9a3e6c1d7b54
Revises: 4f1d8b2c6a90
Create Date: 2026-11-14 12:00:00.000000

O3.3: store the approved O3 membership roles and backfill them, **without
enforcing anything**. Two nullable columns, each with a named CHECK:

- ``organization_memberships.organization_role`` -- ``owner`` | ``admin``
  (``ck_organization_memberships_organization_role``);
- ``branch_assignments.branch_role`` -- ``manager`` | ``staff``
  (``ck_branch_assignments_branch_role``).

The CHECKs accept only those exact lowercase values (or NULL). Their text is
dialect-specific: ``col IN (...)`` on SQLite, whose default comparison is
already byte-exact, and ``CAST(col AS BINARY) IN (...)`` on MySQL, whose
collations are case- and accent-insensitive -- see ``RoleColumn.check_sql``.

Backfill, by a fixed rule from ``users.role`` and nothing else, on rows of
every status (revoked ones stay revoked): an ADMIN's membership becomes
``admin`` and its assignments keep a NULL branch role; a STAFF member's
membership keeps a NULL organization role and its assignments become
``staff``. Pending, inactive and invited accounts are backfilled the same
way -- the stored role decides no access; eligibility is still computed from
the live account. UPDATEs only: no row is inserted, deleted or reactivated,
no id or lifecycle column (status, created_*, revoked_*) changes, no audit
row is written, and no owner or branch manager is created.

**Validation before any DDL.** The O2 data is checked first (R1-R9 below,
each with LEFT JOINs so an orphan row cannot drop out of a join unnoticed)
and every problem is reported at once; nothing is repaired. A refusal
changes nothing.

**Why no batch mode.** SQLite's ``ALTER TABLE`` cannot add a standalone,
table-level CHECK constraint to an existing table (there is no ``ADD
CONSTRAINT``); Alembic does that by rebuilding the table (batch mode), and
rebuilding ``organization_memberships`` fails once ``branch_assignments``
rows reference it (the app turns SQLite foreign keys on for every
connection, so dropping the old table trips their RESTRICT). SQLite *can*,
however, add a new column together with a column-level CHECK in a plain
``ALTER TABLE ... ADD COLUMN ... CONSTRAINT ... CHECK (...)``, with no
rebuild -- so each column is added by a single hand-written ``ALTER TABLE``
carrying its CHECK, and no table is rebuilt. On MySQL the column and its
CHECK are one atomic ``ALTER TABLE ... ADD COLUMN ..., ADD CONSTRAINT ...``.

**Partial DDL.** Each ``ALTER TABLE`` commits on its own (MySQL implicitly;
SQLite under pysqlite too, because DDL does not open a transaction). The
backfill, its verification and Alembic's version stamp share one
transaction after both ALTERs, so a failure leaves at most the role
column(s) present, every value NULL and the version at 4f1d8b2c6a90. R9 then
refuses a retry until the operator follows docs/DATABASE_SETUP_GUIDE.md
section 11.1 -- this migration never drops a column it finds already there.

downgrade() drops the two columns and their CHECKs -- one atomic ``ALTER
TABLE`` per table on MySQL (``DROP CHECK`` + ``DROP COLUMN``), ``DROP
COLUMN`` on SQLite -- skipping a table that is already fully downgraded, so
an interrupted downgrade can be re-run. It refuses, dropping nothing, if a
column and its CHECK disagree or if an ``owner``/``manager`` value exists
(not derivable, so it would be lost). Memberships, assignments and their
lifecycle are untouched; re-upgrading backfills identical values.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a3e6c1d7b54'
down_revision = '4f1d8b2c6a90'
branch_labels = None
depends_on = None

RECOVERY_GUIDE = "docs/DATABASE_SETUP_GUIDE.md section 11.1"
_SUPPORTED_DIALECTS = ("sqlite", "mysql")


class MembershipRoleBackfillRefused(RuntimeError):
    """The O2 data failed validation; nothing was changed."""


class MembershipRoleBackfillFailed(RuntimeError):
    """The backfill did not produce the expected result and was rolled back."""


class MembershipRoleDowngradeRefused(RuntimeError):
    """The role schema is not in a state downgrade() can safely remove."""


class RoleColumn:
    """One role column and its CHECK. A plain class, not a dataclass:
    Alembic loads revision files without registering them in
    ``sys.modules``, which ``dataclasses`` needs."""

    __slots__ = ("table", "column", "check", "values")

    def __init__(self, table: str, column: str, check: str, values: tuple[str, ...]):
        self.table, self.column, self.check, self.values = table, column, check, values

    def __repr__(self) -> str:
        return f"RoleColumn({self.table}.{self.column})"

    def check_sql(self, dialect: str) -> str:
        """The exact-value CHECK text for ``dialect`` -- the same text the
        model emits for that dialect (app/models/organization.py
        ``role_check_sql``; tests/test_membership_roles.py and
        tests/test_migrations.py compare them). Written out here rather than
        imported, so this revision never changes with the application code.

        SQLite compares strings byte by byte by default, so a plain ``IN`` is
        exact there. MySQL compares with the column's collation
        (utf8mb4_unicode_ci in production, utf8mb4_0900_ai_ci on a default
        MySQL 8 server: case- and accent-insensitive), so the value is cast
        to a binary string first and compared byte by byte -- 'Admin',
        'ADMIN', 'ádmin' and 'admin ' are all rejected, whatever the column's
        character set or collation."""
        listed = ", ".join(f"'{v}'" for v in self.values)
        if dialect == "mysql":
            return f"CAST({self.column} AS BINARY) IN ({listed})"
        return f"{self.column} IN ({listed})"


ORGANIZATION_ROLE = RoleColumn("organization_memberships", "organization_role",
                               "ck_organization_memberships_organization_role", ("owner", "admin"))
BRANCH_ROLE = RoleColumn("branch_assignments", "branch_role",
                         "ck_branch_assignments_branch_role", ("manager", "staff"))
ROLE_COLUMNS = (ORGANIZATION_ROLE, BRANCH_ROLE)   # upgrade order; downgrade runs it in reverse

_MEMBERSHIP_LIFECYCLE = ("SELECT id, organization_id, user_id, status, created_at, created_by_id, revoked_at, "
                         "revoked_by_id FROM organization_memberships ORDER BY id")
_ASSIGNMENT_LIFECYCLE = ("SELECT id, organization_id, membership_id, branch_id, status, created_at, created_by_id, "
                         "revoked_at, revoked_by_id FROM branch_assignments ORDER BY id")


# --- schema inspection (read-only) ---------------------------------------------------------------


def column_present(conn, rc: RoleColumn) -> bool:
    return rc.column in {c["name"] for c in sa.inspect(conn).get_columns(rc.table)}


def check_present(conn, rc: RoleColumn) -> bool:
    """Whether ``rc``'s CHECK exists on its table."""
    if conn.dialect.name == "mysql":
        return bool(conn.execute(sa.text(
            "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND CONSTRAINT_TYPE = 'CHECK' AND CONSTRAINT_NAME = :n"),
            {"t": rc.table, "n": rc.check}).scalar())
    return rc.check in {c["name"] for c in sa.inspect(conn).get_check_constraints(rc.table)}


def _check_name_taken(conn, rc: RoleColumn) -> bool:
    """MySQL scopes CHECK names to the schema, so a leftover anywhere would
    make the ADD CONSTRAINT fail; SQLite scopes them to the table."""
    if conn.dialect.name == "mysql":
        return bool(conn.execute(sa.text(
            "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE() "
            "AND CONSTRAINT_TYPE = 'CHECK' AND CONSTRAINT_NAME = :n"), {"n": rc.check}).scalar())
    return check_present(conn, rc)


# --- validation before any DDL (read-only) -------------------------------------------------------


def _who(username, role, is_active=None, staff_approved=None) -> str:
    label = f"'{username}'" if username is not None else "<missing user>"
    detail = role if role is not None else "no account"
    if is_active is not None:
        state = "active" if is_active else "inactive"
        if role == "staff" and not staff_approved:
            state += ", pending approval"
        detail += f", {state}"
    return f"{label} ({detail})"


def find_problems(conn) -> list[str]:
    """Every reason the backfill cannot proceed safely, R1-R9. Reads only."""
    q = lambda sql, **p: conn.execute(sa.text(sql), p).all()  # noqa: E731
    problems: list[str] = []

    # R1 -- the O1/O2 identity: exactly one organization and one branch of it.
    organizations = [r[0] for r in q("SELECT id FROM organizations ORDER BY id")]
    branches = q("SELECT id, organization_id FROM branches ORDER BY id")
    local = None
    if len(organizations) == 1 and len(branches) == 1 and branches[0][1] == organizations[0]:
        local = (organizations[0], branches[0][0])
    else:
        problems.append(
            f"R1 installation identity: expected exactly one organization and one branch of it, found "
            f"{len(organizations)} organization(s) and {len(branches)} branch(es). Fix: check "
            "`flask installation show` and restore a verified backup.")
    org_filter = " AND m.organization_id = :o" if local else ""
    params = {"o": local[0], "b": local[1]} if local else {}

    # R2 -- every ADMIN and STAFF account, in any state, has a membership.
    for r in q("SELECT u.id, u.username, u.role, u.is_active, u.staff_approved FROM users u "
               "LEFT JOIN organization_memberships m ON m.user_id = u.id" + org_filter + " "
               "WHERE u.role IN ('admin', 'staff') AND m.id IS NULL ORDER BY u.id", **params):
        problems.append(
            f"R2 account without membership: user {r[0]} {_who(r[1], r[2], r[3], r[4])}. Fix (previous "
            "release): m = add_member(user, actor=a); assign_branch(m, branch, actor=a).")

    # R3 -- every membership belongs to an existing ADMIN or STAFF account.
    for r in q("SELECT m.id, m.status, m.user_id, u.username, u.role FROM organization_memberships m "
               "LEFT JOIN users u ON u.id = m.user_id "
               "WHERE u.id IS NULL OR u.role NOT IN ('admin', 'staff') ORDER BY m.id"):
        what = "references a missing user" if r[4] is None else f"belongs to a {r[4]} account"
        problems.append(
            f"R3 membership {r[0]} ({r[1]}) {what}: user {r[2]} {_who(r[3], r[4])}. No O2 service can fix "
            "this. Fix: restore a verified backup.")

    # R4 -- every membership is in the local organization.
    for r in q("SELECT m.id, m.organization_id, m.user_id FROM organization_memberships m "
               "LEFT JOIN organizations o ON o.id = m.organization_id WHERE o.id IS NULL"
               + (" OR m.organization_id <> :o" if local else "") + " ORDER BY m.id", **params):
        problems.append(
            f"R4 membership {r[0]} (user {r[2]}) is in organization {r[1]}, which is missing or not this "
            "installation's. Fix: restore a verified backup.")

    # R5 -- no orphan assignment: its membership must exist.
    for r in q("SELECT a.id, a.membership_id, a.status FROM branch_assignments a "
               "LEFT JOIN organization_memberships m ON m.id = a.membership_id WHERE m.id IS NULL ORDER BY a.id"):
        problems.append(
            f"R5 orphan assignment {r[0]} ({r[2]}): membership {r[1]} does not exist. Fix: restore a verified "
            "backup.")

    # R6 -- every assignment's organization and branch match its membership
    # and this installation.
    local_clause = " OR a.organization_id <> :o OR a.branch_id <> :b" if local else ""
    for r in q("SELECT a.id, a.membership_id, a.organization_id, m.organization_id, a.branch_id, b.id, "
               "b.organization_id FROM branch_assignments a "
               "LEFT JOIN organization_memberships m ON m.id = a.membership_id "
               "LEFT JOIN branches b ON b.id = a.branch_id "
               "WHERE m.id IS NOT NULL AND (a.organization_id <> m.organization_id OR b.id IS NULL "
               "OR b.organization_id <> a.organization_id" + local_clause + ") ORDER BY a.id", **params):
        branch = "missing" if r[5] is None else f"of organization {r[6]}"
        problems.append(
            f"R6 assignment {r[0]} (membership {r[1]}): organization {r[2]}, membership's organization {r[3]}, "
            f"branch {r[4]} ({branch}) do not match this installation. Fix: restore a verified backup.")

    # R7 -- every membership, any status, any role, has an assignment row to
    # the local branch (needs R1's branch).
    if local:
        for r in q("SELECT m.id, m.status, m.user_id, u.username, u.role FROM organization_memberships m "
                   "LEFT JOIN users u ON u.id = m.user_id "
                   "LEFT JOIN branch_assignments a ON a.membership_id = m.id AND a.branch_id = :b "
                   "WHERE a.id IS NULL ORDER BY m.id", **params):
            fix = ("assign_branch(m, branch, actor=a)" if r[1] == "active" else
                   "reinstate_membership(m, actor=a); assign_branch(m, branch, actor=a); "
                   "revoke_membership(m, actor=a) -- or restore a verified backup")
            problems.append(
                f"R7 membership {r[0]} ({r[1]}) of user {r[2]} {_who(r[3], r[4])} has no branch assignment row. "
                f"Fix (previous release): {fix}.")

    # R8 -- no active assignment under a revoked membership.
    for r in q("SELECT a.id, a.membership_id, m.user_id, u.username FROM branch_assignments a "
               "LEFT JOIN organization_memberships m ON m.id = a.membership_id "
               "LEFT JOIN users u ON u.id = m.user_id "
               "WHERE m.id IS NOT NULL AND a.status = 'active' AND m.status = 'revoked' ORDER BY a.id"):
        problems.append(
            f"R8 active assignment {r[0]} under revoked membership {r[1]} (user {r[2]} '{r[3]}'). Fix "
            "(previous release): revoke_assignment(assignment, actor=a).")

    # R9 -- no role column or CHECK left behind by an earlier attempt.
    for rc in ROLE_COLUMNS:
        found = [what for what, there in (("column", column_present(conn, rc)),
                                          ("CHECK constraint", _check_name_taken(conn, rc))) if there]
        if found:
            problems.append(
                f"R9 {rc.table}.{rc.column}: {' and '.join(found)} already present (an earlier attempt stopped "
                f"part-way?). This migration never drops it. Fix: the partial-DDL procedure in {RECOVERY_GUIDE}.")
    return problems


def check_preconditions(conn) -> None:
    problems = find_problems(conn)
    if problems:
        raise MembershipRoleBackfillRefused(
            f"O3.3 membership-role migration refused: {len(problems)} problem(s). Nothing was changed.\n"
            "Repair on the previous release (O3.3's code cannot run on this schema) after taking and "
            f"verifying a backup, with the app stopped; see {RECOVERY_GUIDE}. Each service call commits or "
            "rolls back the whole session, so call it from a clean session and pass an active admin as actor.\n"
            + "\n".join(f"  - {p}" for p in problems))


# --- DDL -----------------------------------------------------------------------------------------


def add_role_column(conn, rc: RoleColumn) -> None:
    """One ALTER TABLE adding the column with its CHECK: atomic per table on
    MySQL; no table rebuild on SQLite."""
    if conn.dialect.name == "mysql":
        conn.execute(sa.text(f"ALTER TABLE {rc.table} ADD COLUMN {rc.column} VARCHAR(16) NULL, "
                             f"ADD CONSTRAINT {rc.check} CHECK ({rc.check_sql('mysql')})"))
    else:
        conn.execute(sa.text(f"ALTER TABLE {rc.table} ADD COLUMN {rc.column} VARCHAR(16) "
                             f"CONSTRAINT {rc.check} CHECK ({rc.check_sql('sqlite')})"))


def drop_role_column(conn, rc: RoleColumn) -> None:
    """Removes exactly ``rc``'s column and CHECK: one atomic ALTER TABLE on
    MySQL; on SQLite the column-level CHECK goes with its column."""
    if conn.dialect.name == "mysql":
        conn.execute(sa.text(f"ALTER TABLE {rc.table} DROP CHECK {rc.check}, DROP COLUMN {rc.column}"))
    else:
        conn.execute(sa.text(f"ALTER TABLE {rc.table} DROP COLUMN {rc.column}"))


# --- backfill ------------------------------------------------------------------------------------


def backfill(conn) -> None:
    conn.execute(sa.text("UPDATE organization_memberships SET organization_role = 'admin' "
                         "WHERE user_id IN (SELECT id FROM users WHERE role = 'admin')"))
    conn.execute(sa.text("UPDATE branch_assignments SET branch_role = 'staff' WHERE membership_id IN "
                         "(SELECT m.id FROM organization_memberships m JOIN users u ON u.id = m.user_id "
                         "WHERE u.role = 'staff')"))


def verify_backfill(conn, memberships_before, assignments_before) -> None:
    """Every row holds exactly the role its account implies (LEFT JOINs and
    CASE, so a row with no account counts as wrong rather than vanishing),
    and no id, row or lifecycle value changed."""
    problems = []
    wrong_memberships = conn.execute(sa.text(
        "SELECT COUNT(*) FROM organization_memberships m LEFT JOIN users u ON u.id = m.user_id "
        "WHERE CASE WHEN (u.role = 'admin' AND m.organization_role = 'admin') "
        "OR (u.role = 'staff' AND m.organization_role IS NULL) THEN 0 ELSE 1 END = 1")).scalar()
    wrong_assignments = conn.execute(sa.text(
        "SELECT COUNT(*) FROM branch_assignments a "
        "LEFT JOIN organization_memberships m ON m.id = a.membership_id LEFT JOIN users u ON u.id = m.user_id "
        "WHERE CASE WHEN (u.role = 'staff' AND a.branch_role = 'staff') "
        "OR (u.role = 'admin' AND a.branch_role IS NULL) THEN 0 ELSE 1 END = 1")).scalar()
    if wrong_memberships:
        problems.append(f"{wrong_memberships} membership(s) without the expected organization role")
    if wrong_assignments:
        problems.append(f"{wrong_assignments} assignment(s) without the expected branch role")
    if conn.execute(sa.text(_MEMBERSHIP_LIFECYCLE)).all() != memberships_before:
        problems.append("membership rows, ids or lifecycle values changed")
    if conn.execute(sa.text(_ASSIGNMENT_LIFECYCLE)).all() != assignments_before:
        problems.append("assignment rows, ids or lifecycle values changed")
    if problems:
        raise MembershipRoleBackfillFailed(
            "O3.3 membership-role backfill verification failed: " + "; ".join(problems) + ". The backfill was "
            f"rolled back, but the role column(s) may remain: follow {RECOVERY_GUIDE} before retrying.")


# --- upgrade / downgrade -------------------------------------------------------------------------


def _require_supported_dialect(conn) -> None:
    if conn.dialect.name not in _SUPPORTED_DIALECTS:
        raise MembershipRoleBackfillRefused(f"unsupported database dialect {conn.dialect.name!r}")


def upgrade():
    conn = op.get_bind()
    _require_supported_dialect(conn)
    # 1. Validate everything before the first DDL statement: each ALTER
    #    commits on its own, so refusing later would leave a partial schema.
    check_preconditions(conn)
    memberships_before = conn.execute(sa.text(_MEMBERSHIP_LIFECYCLE)).all()
    assignments_before = conn.execute(sa.text(_ASSIGNMENT_LIFECYCLE)).all()
    # 2-3. One ALTER TABLE per table.
    for rc in ROLE_COLUMNS:
        add_role_column(conn, rc)
    # 4-5. Backfill and verify, in the transaction Alembic stamps the version in.
    backfill(conn)
    verify_backfill(conn, memberships_before, assignments_before)


def downgrade():
    conn = op.get_bind()
    _require_supported_dialect(conn)
    states = {rc: (column_present(conn, rc), check_present(conn, rc)) for rc in ROLE_COLUMNS}
    problems = []
    for rc, (has_column, has_check) in states.items():
        if has_column != has_check:
            problems.append(f"{rc.table}: column {rc.column} {'present' if has_column else 'absent'} but CHECK "
                            f"{rc.check} {'present' if has_check else 'absent'}")
    if states[ORGANIZATION_ROLE][0]:
        owners = conn.execute(sa.text(
            "SELECT COUNT(*) FROM organization_memberships WHERE organization_role = 'owner'")).scalar()
        if owners:
            problems.append(f"{owners} membership(s) hold the owner role, which cannot be recomputed")
    if states[BRANCH_ROLE][0]:
        managers = conn.execute(sa.text(
            "SELECT COUNT(*) FROM branch_assignments WHERE branch_role = 'manager'")).scalar()
        if managers:
            problems.append(f"{managers} assignment(s) hold the manager role, which cannot be recomputed")
    if problems:
        raise MembershipRoleDowngradeRefused(
            "O3.3 downgrade refused, nothing dropped: " + "; ".join(problems) + f". See {RECOVERY_GUIDE}.")
    for rc in reversed(ROLE_COLUMNS):
        if states[rc][0]:          # already fully downgraded otherwise: skip, so a re-run completes
            drop_role_column(conn, rc)
