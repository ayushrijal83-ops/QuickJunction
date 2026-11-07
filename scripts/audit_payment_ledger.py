"""Quick Junction -- permanent, read-only payment-ledger integrity audit (M7.5.4.9).

    python scripts/audit_payment_ledger.py --database-url mysql+pymysql://...

Answers, on demand, "is payment_ledger_entries internally consistent, and
does it agree with Order.amount_settled and the legacy payments table" --
as a standing operator/CI health check, not a one-time migration tool. This
script never writes anything: no inserts, updates, deletes, locks, or
repairs. It does not call, import the write paths of, or change the
behavior of scripts/reconcile_payment_ledger.py in any way -- it is a pure
read-only consumer of the same schema that script writes to.

Exit code: 0 if no findings, 1 if any integrity finding was raised (so this
is usable directly as a cron/CI health check), 2 if no --database-url/
DATABASE_URL was given, 1 for a malformed --database-url or a disallowed
target (same as scripts/reconcile_payment_ledger.py's own conventions).

Checks performed
-----------------
A1  A ledger 'payment' entry's own ``refunded_amount`` projection must equal
    the sum of its linked 'refund' entries (``reversed_entry_id`` pointing
    at it). Ledger-internal self-consistency; the live write path
    (app/services/payment_ledger.py's refund_ledger_payment) maintains this
    with one conditional UPDATE plus one INSERT in the same transaction, so
    a mismatch means something wrote around that path.

A2  A 'refund'/'correction' entry's ``reversed_entry_id`` must resolve to an
    existing row (checked anyway as defense in depth -- the FK already
    enforces this structurally; see "missing" below) whose own
    ``entry_type`` is 'payment'. The PaymentLedgerEntry model's own
    docstring says the FK "only guarantees the referenced row exists -- not
    that it is itself a payment entry. That check ... belongs to the future
    service layer" -- this is that check, made permanent.

A3  A 'refund'/'correction' entry's ``order_id`` must match the ``order_id``
    of the payment entry it reverses. Nothing in the schema ties these
    together either (same FK, same gap, different axis). A2's "wrong type"
    case and A3 are checked independently of each other once a target row
    exists -- a single entry that is both the wrong type *and* belongs to
    the wrong order raises both findings, never only the first one found.

A4  ``Order.amount_settled`` must equal the sum of ``amount`` across every
    one of its ledger entries (payment positive, refund negative,
    adjustment/correction signed either way -- this is exactly how
    record_ledger_payment/refund_ledger_payment maintain it incrementally,
    via ``amount_settled = amount_settled +/- amount``). Covers the
    zero-ledger-entries case via COALESCE(SUM(...), 0) -- see C1 for why
    that specific shape gets its own, more specific label instead of being
    folded into A4.

B2  For an order with a legacy ``payments`` row that already has *some*
    ledger activity (at least one ledger entry -- otherwise see "awaiting
    reconciliation" below, not a finding): ``payments.refunded_amount`` must
    never exceed the total 'refund'-entry amount recorded anywhere on that
    order's ledger. This is one-directional on purpose -- see "Why B2 is
    directional" below.

C1  ``Order.amount_settled`` is nonzero but the order has zero
    payment_ledger_entries rows at all. Structurally should be impossible
    post-M7.5.1 (nothing else writes amount_settled), so this is split out
    from A4 as its own, more specific and more severe label: it means
    amount_settled was set by something that bypassed the ledger entirely,
    not just that the ledger's own numbers don't add up.

Informational, never a finding: an order whose legacy ``payments`` row has
no corresponding ledger 'payment' entry at all is "awaiting reconciliation"
-- the well-documented, expected M7.5.1-to-M7.5.3 gap
scripts/reconcile_payment_ledger.py exists to close. Counted and reported
separately, never mixed into the integrity findings (see "Why not
scan()" below for why this is a small independent query rather than an
import of that script's own scan()).

Why B2 is directional
----------------------
``refund_ledger_payment`` never touches the legacy ``payments`` table (it
was retired as a live writer in M7.5.4.7) -- so once an order is
reconciled, ``payments.refunded_amount`` is a frozen historical snapshot,
and the ledger going on to record *more* refund activity afterward (a
perfectly normal, expected thing to happen) will make the ledger's refund
total exceed that frozen snapshot. That is correct, not corruption, and B2
must never flag it. The real, historical risk this rule exists to catch is
the other direction: ``tests/test_mysql_concurrency.py``'s own
``test_reconciliation_payment_lock_is_real_against_a_concurrent_legacy_refund``
documents that *before* M7.5.4.7 retired the legacy writer, a legacy refund
could land on an order's ``payments`` row *after* that order's ledger entry
had already been backfilled -- a refund the ledger has no record of at all.
That shows up only as a *shortfall* (legacy says more was refunded than the
ledger has ever recorded), never as the ledger recording more than the
legacy snapshot -- hence the one-directional ``>`` comparison, not ``!=``.

Why not scan()
----------------
scripts/reconcile_payment_ledger.py's own ``scan()`` already computes
exactly this "awaiting reconciliation" count, and importing it is safe on
its own terms -- the module has no import-time side effects (every
app/database access inside it is a local import inside a function body,
never executed just by importing the module), and ``scan()`` itself never
writes anything. It was not reused anyway, for one concrete reason: it
hardcodes ``from app.extensions import db; ... db.session...`` -- it has no
way to accept an injected connection. This audit's own read-only strategy
(below) needs every query, including this one, to go through whichever
executor ``_read_only_executor`` picked for the current dialect; routing
one query through ``scan()``'s own hardcoded session while every other
query goes through an isolated connection would silently reintroduce the
exact ambient-session coupling this script exists to avoid. Duplicating
one simple COUNT query here is a smaller, more honest cost than threading
an executor parameter through a script this milestone must not modify.

Read-only design: why the strategy differs by dialect
---------------------------------------------------------
The intent is: never touch the transaction boundary of whatever ambient
``db.session`` a caller (a long-running process, or a test) might already
have open, and never let this script's own reads trigger an autoflush of
a caller's pending, uncommitted ORM changes.

For MySQL, this is straightforward: ``engine.connect()`` checks out a
genuinely separate physical connection from the pool, with its own
transaction, completely isolated from whatever the caller's session is
doing on its own, different connection -- so every query here runs on that
separate connection, and it is rolled back (never committed) when done,
which cannot affect anything else. See "MySQL snapshot isolation" below for
the isolation-level guarantee layered on top of that separation.

SQLite is different, and this was verified empirically rather than
assumed: this application's test configuration binds ``sqlite://`` through
SQLAlchemy's ``StaticPool``, which hands out the exact same single
physical connection to every caller. A quick, deliberate check (open a
second "connection" via ``engine.connect()`` while an ambient session has
an uncommitted write pending, then call ``.rollback()`` on that "second"
connection) showed the ambient session's uncommitted work was discarded --
because there was never a second connection or transaction to begin with,
just a second Python wrapper around the same one. Opening a "separate"
SQLite connection here would therefore not be isolation at all, and the
most natural way of using it (an explicit rollback on close) would risk
exactly what this script must never do: discarding a caller's own pending
work. For SQLite, this script instead reads through the ambient
``db.session`` itself, wrapped in ``session.no_autoflush`` (verified,
empirically, to leave pending/dirty ORM state completely untouched across
the read) and never calls ``commit()``/``rollback()``/``expire_all()`` on
it at all -- the caller's session is left in exactly the state it was
found in.

MySQL snapshot isolation: why every check sees one coherent point in time
------------------------------------------------------------------------------
A connection being separate from the caller's own session (above) is not,
by itself, a guarantee that *this script's own* six sequential queries see
one consistent snapshot of the data as they run one after another --
InnoDB's per-statement consistent read only guarantees that *within* a
single statement, and whether that extends across multiple statements in
one transaction depends entirely on the session's isolation level.
``_read_only_executor``'s MySQL branch does not assume the server's
default (typically, but not guaranteed to be, REPEATABLE READ) is what is
actually configured -- a server or a pooler/proxy in front of it reconfigured
for READ COMMITTED would otherwise let a concurrent commit (a refund
landing between this run's A1 query and its A4/C1 query, say) produce a
report whose own numbers were computed moments apart, with nothing in the
output to say so.

Instead, immediately after ``engine.connect()`` -- before any statement has
run on that connection -- ``conn.execution_options(isolation_level=
"REPEATABLE READ")`` pins this one connection's *session-level* isolation
(SQLAlchemy issues ``SET SESSION TRANSACTION ISOLATION LEVEL ...`` for
this, never ``SET GLOBAL``, so no other connection anywhere, including a
caller's own ambient session, is ever affected by this). The very next
statement -- ``SELECT @@transaction_isolation`` -- begins the one
transaction every later query in this run executes inside (autobegin on
first statement, held open with no intermediate commit/rollback until this
context manager's own final ``conn.rollback()``) and reads back what the
server actually honored, raising ``AuditIsolationError`` immediately if it
is not REPEATABLE READ rather than silently proceeding to produce a report
that claims a coherent snapshot it does not have. That verification query
only reads a server-layer system variable, though, not an InnoDB table --
it begins the transaction, but does not itself establish InnoDB's
consistent-read snapshot. InnoDB only takes that snapshot at the first
statement that actually *reads an InnoDB table* inside the transaction,
which here is A1's query (``_A1_SQL``, the first of the six checks). From
that point on, the guarantee is exactly what matters: A1 through the
awaiting-reconciliation count all run inside the same, already-open
transaction, so every one of them after A1 shares that same snapshot --
a concurrent write committed by anything else, at any point from A1's own
query onward, is invisible to every one of this run's remaining queries,
exactly as REPEATABLE READ promises. (The verification query itself cannot
be made part of that snapshot -- it runs before the snapshot exists, by
construction -- but it reads no table data, so there is nothing for it to
see inconsistently.)

Known, accepted limitation: InnoDB's consistent snapshot is taken at A1's
query, not at the moment the operator ran the command, and not at the
``SELECT @@transaction_isolation`` verification query that precedes it --
an audit run can never see a write that was still in-flight (uncommitted)
when A1 ran, by design (that write is either fully reflected or not
reflected at all, never half of each, which is the actual guarantee being
made). It also cannot retroactively notice a write that commits *after*
A1's query but before the run finishes -- that write is correctly
invisible to this run and will be picked up by the next one. The narrow
exception is the gap between the verification query and A1's query
itself: on the very rare chance something commits in that gap, it will
still correctly appear in A1 and every later query (nothing is torn
between them), it is just not guaranteed to be *excluded* the way a write
committed after A1 is.

Read-only database credentials
--------------------------------
Everything above is an application-level guarantee -- this script issues
no write statements. As defense in depth, operators who can provision a
dedicated MySQL account with only SELECT privileges (a read-only grant,
distinct from the ``qj_user``/``qj_migrator`` accounts scripts/db_setup.py
manages) are encouraged to point --database-url at one for this tool
specifically, the same way scripts/backup_database.py recommends a
minimally-privileged account for its own read-only dump step. Nothing here
requires it -- it is a recommendation, not a precondition this script
checks -- because this application does not yet define such an account
(out of this milestone's scope to add one).

Out of scope, with reasons (nothing here is an oversight)
--------------------------------------------------------------
- More than one 'payment' entry per order, or more than one 'refund' entry
  per payment entry: both are normal under the live ledger service, which
  explicitly supports partial/multiple payments and partial/multiple
  refunds. ``migrations/versions/e39e6dffcb5d_add_payment_ledger.py``'s own
  ``_preflight_downgrade`` flags both -- but only because the *legacy*
  schema cannot represent them, a downgrade-compatibility concern, not a
  ledger-integrity one. Not reused here.
- The CHECK constraints already on ``payment_ledger_entries``/``orders``
  (amount sign, projection-field presence, effective-nonnegative, ...):
  already enforced by the database itself at write time on both SQLite and
  MySQL. Re-checking them here would only matter if someone bypassed the
  ORM and the database's own constraints via raw, unchecked SQL -- outside
  this tool's threat model.
- A "duplicate backfill" detector keyed on matching
  ``scripts/reconcile_payment_ledger.py``'s ``reason`` string: too fragile
  (matching application-generated text). A real duplicate-backfill bug
  would still surface through A1 and/or A4.
- Any business-rule validation of ``corrected_amount``/'adjustment'/
  'correction' entries beyond what A4's sum already covers: nothing in the
  live application writes a nonzero ``corrected_amount`` or an
  'adjustment'/'correction' entry yet (confirmed by inspection -- only test
  fixtures create them, to exercise the CHECK constraints and the
  migration's downgrade refusal). There is no live business rule yet to
  validate beyond that sum.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@dataclass
class AuditFinding:
    rule: str
    order_id: int | None
    detail: str


@dataclass
class AuditReport:
    findings: list[AuditFinding] = field(default_factory=list)
    orders_checked: int = 0
    awaiting_reconciliation_count: int = 0

    def add(self, rule: str, order_id: int | None, detail: str) -> None:
        self.findings.append(AuditFinding(rule, order_id, detail))

    def by_rule(self, rule: str) -> list[AuditFinding]:
        return [f for f in self.findings if f.rule == rule]

    @property
    def is_clean(self) -> bool:
        return not self.findings


def _dec(value) -> Decimal:
    """Coerce a raw query result to Decimal exactly. Every numeric column
    below is already declared via TextClause.columns(..., sa.Numeric(10, 2))
    specifically so the driver hands back a Decimal already (verified
    empirically for SQLite's raw sa.text() path, which otherwise returns a
    bare float) -- this is belt-and-suspenders on top of that, not the
    primary guarantee, so a future query that forgets .columns() fails
    loudly here rather than silently comparing through float."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class AuditIsolationError(RuntimeError):
    """Raised when a coherent read-only snapshot cannot be established for
    the target dialect. Never caught internally -- a caller seeing this
    gets a clear, loud failure instead of a report that silently claims a
    consistency guarantee it does not actually have."""


@contextlib.contextmanager
def _read_only_executor(engine):
    """Yields an object with a plain .execute(stmt) method -- either the
    ambient db.session (SQLite) or a dedicated, isolation-level-pinned
    Connection (MySQL). See the module docstring's "Read-only design" and
    "MySQL snapshot isolation" sections for why these differ, how the
    isolation level is established and verified before the first real
    query, and how that was verified empirically, not assumed.

    Every dialect this application actually supports is handled explicitly;
    anything else raises AuditIsolationError rather than silently falling
    through to a connection this function has no verified isolation story
    for."""
    if engine.dialect.name == "sqlite":
        from app.extensions import db

        with db.session.no_autoflush:
            yield db.session
    elif engine.dialect.name == "mysql":
        with engine.connect() as conn:
            # Pins this ONE connection's session-level isolation -- SQLAlchemy
            # issues "SET SESSION TRANSACTION ISOLATION LEVEL ..." on it, never
            # SET GLOBAL, so no other connection anywhere (including the
            # caller's own ambient session, if any) is affected. Must happen
            # before any statement runs on this connection -- done immediately
            # after connect(), before even the verification query below.
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
            # The verification query IS the first statement on this
            # connection, so it both begins the shared transaction every
            # later A1/A2/A3/A4/B2/awaiting-count query in this run also
            # executes inside (one BEGIN, held open until the rollback()
            # below -- never a fresh transaction per query) and proves the
            # isolation level actually took effect, rather than trusting
            # execution_options() silently. MySQL 8.0's system variable is
            # "transaction_isolation" (the pre-8.0 "tx_isolation" name was
            # removed outright) -- matches this project's MySQL 8.0 CI/test
            # baseline, so no legacy-name fallback is implemented.
            reported = conn.execute(sa.text("SELECT @@transaction_isolation")).scalar()
            normalized = (reported or "").strip().upper().replace("-", " ").replace("_", " ")
            if normalized != "REPEATABLE READ":
                raise AuditIsolationError(
                    f"Could not establish REPEATABLE READ for the audit's MySQL connection "
                    f"(server reports {reported!r} after requesting it). Refusing to produce "
                    "a report that would silently claim a coherent snapshot across its checks "
                    "when it may not have one."
                )
            yield conn
            conn.rollback()  # never committed anything; explicit for clarity
    else:
        raise AuditIsolationError(
            f"scripts/audit_payment_ledger.py has no verified read-only isolation strategy for "
            f"the {engine.dialect.name!r} dialect -- only sqlite and mysql are supported."
        )


# --- A1: a payment entry's refunded_amount projection vs. its own refunds --------------------

_A1_SQL = sa.text("""
    SELECT l.id AS entry_id, l.order_id AS order_id,
           COALESCE(l.refunded_amount, 0) AS projected,
           COALESCE((
               SELECT SUM(-r.amount) FROM payment_ledger_entries r
               WHERE r.entry_type = 'refund' AND r.reversed_entry_id = l.id
           ), 0) AS actual
    FROM payment_ledger_entries l
    WHERE l.entry_type = 'payment'
""").columns(projected=sa.Numeric(10, 2), actual=sa.Numeric(10, 2))


def _check_a1(executor, report: AuditReport) -> None:
    """Symmetric on purpose: a projection that over-reports its actual
    linked refunds and one that under-reports them are both real mismatches
    (tested both ways; see tests/test_payment_ledger_audit.py)."""
    for row in executor.execute(_A1_SQL).all():
        projected, actual = _dec(row.projected), _dec(row.actual)
        if projected != actual:
            report.add(
                "A1_REFUND_PROJECTION_MISMATCH", row.order_id,
                f"ledger payment entry id={row.entry_id}: refunded_amount projection is "
                f"{projected}, but its linked refund entries sum to {actual}",
            )


# --- A2/A3: reversed_entry_id targeting a missing row, the wrong type, or the wrong order ----

_A2_A3_SQL = sa.text("""
    SELECT c.id AS entry_id, c.order_id AS order_id, c.entry_type AS entry_type,
           c.reversed_entry_id AS reversed_entry_id,
           t.id AS target_id, t.entry_type AS target_type, t.order_id AS target_order_id
    FROM payment_ledger_entries c
    LEFT JOIN payment_ledger_entries t ON t.id = c.reversed_entry_id
    WHERE c.entry_type IN ('refund', 'correction')
""")


def _check_a2_a3(executor, report: AuditReport) -> None:
    """Missing is its own, exclusive branch -- there is no target row to
    examine a type or an order_id on. Once a target does exist, wrong-type
    and cross-order are deliberately independent checks (not elif-chained):
    a single entry can be both at once (it reverses some *other* order's
    adjustment entry, say), and both are independently real, independently
    actionable findings -- reporting only the first one found would
    silently hide the other."""
    for row in executor.execute(_A2_A3_SQL).all():
        if row.target_id is None:
            report.add(
                "A2_MISSING_REVERSAL_TARGET", row.order_id,
                f"{row.entry_type} entry id={row.entry_id} names reversed_entry_id="
                f"{row.reversed_entry_id}, which does not exist (the FK already makes this "
                "impossible under normal operation -- checked anyway as defense in depth)",
            )
            continue
        if row.target_type != "payment":
            report.add(
                "A2_WRONG_REVERSAL_TARGET_TYPE", row.order_id,
                f"{row.entry_type} entry id={row.entry_id} reverses entry id={row.target_id}, "
                f"whose entry_type is {row.target_type!r}, not 'payment'",
            )
        if row.target_order_id != row.order_id:
            report.add(
                "A3_CROSS_ORDER_REVERSAL", row.order_id,
                f"{row.entry_type} entry id={row.entry_id} (order_id={row.order_id}) reverses "
                f"payment entry id={row.target_id} belonging to a different order "
                f"(order_id={row.target_order_id})",
            )


# --- A4/C1: Order.amount_settled vs. the ledger-derived net settlement -----------------------
#
# One pre-aggregated GROUP BY (computed once, over the whole table) joined
# back to every order, rather than two separate correlated subqueries
# (SUM and COUNT) re-scanning payment_ledger_entries once each per order
# row -- same result, half the per-order scanning. COALESCE still covers an
# order with no matching aggregate row at all (the LEFT JOIN leaves both
# columns NULL), identically to the zero-entry case the two-subquery form
# handled before.

_A4_C1_SQL = sa.text("""
    SELECT o.id AS order_id, o.amount_settled AS amount_settled,
           COALESCE(agg.ledger_sum, 0) AS ledger_sum,
           COALESCE(agg.entry_count, 0) AS entry_count
    FROM orders o
    LEFT JOIN (
        SELECT order_id, SUM(amount) AS ledger_sum, COUNT(*) AS entry_count
        FROM payment_ledger_entries
        GROUP BY order_id
    ) agg ON agg.order_id = o.id
""").columns(amount_settled=sa.Numeric(10, 2), ledger_sum=sa.Numeric(10, 2))


def _check_a4_c1(executor, report: AuditReport) -> None:
    for row in executor.execute(_A4_C1_SQL).all():
        report.orders_checked += 1
        settled, ledger_sum = _dec(row.amount_settled), _dec(row.ledger_sum)
        if settled == ledger_sum:
            continue
        if row.entry_count == 0:
            report.add(
                "C1_SETTLED_WITH_NO_LEDGER_ENTRIES", row.order_id,
                f"amount_settled is {settled}, but this order has zero payment_ledger_entries rows",
            )
        else:
            report.add(
                "A4_SETTLEMENT_MISMATCH", row.order_id,
                f"amount_settled is {settled}, but its ledger entries sum to {ledger_sum}",
            )


# --- B2: legacy payments.refunded_amount vs. the order's ledger-recorded refund total ---------

_B2_SQL = sa.text("""
    SELECT p.order_id AS order_id, p.refunded_amount AS legacy_refunded,
           COALESCE((
               SELECT SUM(-r.amount) FROM payment_ledger_entries r
               WHERE r.entry_type = 'refund' AND r.order_id = p.order_id
           ), 0) AS ledger_refund_total
    FROM payments p
    WHERE EXISTS (SELECT 1 FROM payment_ledger_entries l WHERE l.order_id = p.order_id)
""").columns(legacy_refunded=sa.Numeric(10, 2), ledger_refund_total=sa.Numeric(10, 2))


def _check_b2(executor, report: AuditReport) -> None:
    for row in executor.execute(_B2_SQL).all():
        legacy_refunded, ledger_total = _dec(row.legacy_refunded), _dec(row.ledger_refund_total)
        if legacy_refunded > ledger_total:
            report.add(
                "B2_LEGACY_REFUND_SHORTFALL", row.order_id,
                f"legacy payments.refunded_amount is {legacy_refunded}, but this order's ledger "
                f"refund entries total only {ledger_total} -- a legacy refund the ledger never "
                "learned about (the pre-M7.5.4.7 gap; see the module docstring)",
            )


# --- informational: legacy payments row awaiting reconciliation (never a finding) -------------

_AWAITING_RECONCILIATION_SQL = sa.text("""
    SELECT COUNT(*) FROM payments p
    WHERE NOT EXISTS (
        SELECT 1 FROM payment_ledger_entries l
        WHERE l.order_id = p.order_id AND l.entry_type = 'payment'
    )
""")


def _count_awaiting_reconciliation(executor) -> int:
    return executor.execute(_AWAITING_RECONCILIATION_SQL).scalar() or 0


def run_audit(engine) -> AuditReport:
    """The single public entry point. Runs every check above through
    whichever read-only executor fits ``engine``'s dialect and returns a
    complete AuditReport -- never writes anything."""
    report = AuditReport()
    with _read_only_executor(engine) as executor:
        _check_a1(executor, report)
        _check_a2_a3(executor, report)
        _check_a4_c1(executor, report)
        _check_b2(executor, report)
        report.awaiting_reconciliation_count = _count_awaiting_reconciliation(executor)
    return report


def _print_report(report: AuditReport) -> None:
    from scripts._backup_common import ok, say, step, warn

    step(f"Orders checked: {report.orders_checked}")

    step("Awaiting reconciliation (legacy payment, no ledger entry yet -- not a defect)")
    say(f"    {report.awaiting_reconciliation_count} order(s)")

    if report.is_clean:
        step("Integrity findings")
        ok("none -- the ledger is internally consistent with Order.amount_settled and the "
           "legacy payments table")
        return

    step(f"Integrity findings ({len(report.findings)})")
    for finding in report.findings:
        warn(f"[{finding.rule}] order #{finding.order_id}: {finding.detail}")

    counts: dict[str, int] = {}
    for finding in report.findings:
        counts[finding.rule] = counts.get(finding.rule, 0) + 1
    step("Summary by rule")
    for rule, count in sorted(counts.items()):
        say(f"    {rule}: {count}")


def main(argv: list[str] | None = None) -> int:
    from scripts._backup_common import BackupError, is_disposable_test_name, say, validate_database_name

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument(
        "--allow-production-target", action="store_true",
        help="required (for a non-SQLite --database-url) if the database name does not end in "
             "'_test'. This tool is read-only; the gate is applied anyway, purely so a long "
             "table-scan cannot land on the wrong environment by habit.",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        say("No DATABASE_URL and no --database-url given.")
        return 2

    # Must happen before `from app import create_app` below -- see
    # scripts/reconcile_payment_ledger.py's own main() for why.
    os.environ["DATABASE_URL"] = args.database_url

    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        url = make_url(args.database_url)
    except (ArgumentError, ValueError):
        say("[FAILED] --database-url is not a valid database URL (value not shown).")
        return 1

    if url.get_backend_name() != "sqlite":
        name = url.database or ""
        try:
            validate_database_name(name)
        except BackupError as exc:
            say(f"[FAILED] {exc}")
            return 1
        if not is_disposable_test_name(name) and not args.allow_production_target:
            say(f"[FAILED] {name!r} does not end in '_test'. Auditing it requires "
                "--allow-production-target.")
            return 1

    from app import create_app
    app = create_app()
    with app.app_context():
        from app.extensions import db

        try:
            report = run_audit(db.engine)
        except AuditIsolationError as exc:
            say(f"[FAILED] {exc}")
            return 1

    _print_report(report)
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
