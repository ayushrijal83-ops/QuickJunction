"""Quick Junction -- payment-ledger legacy-data reconciliation (M7.5.4).

    python scripts/reconcile_payment_ledger.py
    python scripts/reconcile_payment_ledger.py --database-url mysql+pymysql://...
    python scripts/reconcile_payment_ledger.py --database-url ... \\
        --apply --confirm "RECONCILE <n>"

Backfills a 'payment' ledger entry (and, where the legacy row was refunded, a
matching 'refund' entry) for every order that was paid through the legacy
``app/services/payments.py`` route *after* the M7.5.1 migration's own
one-time backfill ran but *before* the M7.5.3 cutover actually deployed --
see ``app/services/payment_ledger.py``'s "Cross-path note" and
``app/templates/staff/order_detail.html``'s ``unreconciled_legacy_payment``
guard, which this script is the other half of. **That guard stays in place**
after this script runs; removing it is a separate, later decision made only
once its absence has actually been verified against real data (see
docs/PRODUCTION_READINESS.md).

Why this is not just "re-run the migration's backfill SQL again"
------------------------------------------------------------------
``migrations/versions/e39e6dffcb5d_add_payment_ledger.py``'s backfill runs
once, offline, with no concurrent writers -- a standalone tool run against a
live, serving database cannot assume either of those:

  - Its settlement step (``_BACKFILL_SETTLEMENT_SQL``) recomputes
    ``amount_settled`` *unconditionally* for every order with a legacy
    ``payments`` row. That is safe only because, at migration time, nothing
    else has ever written to ``amount_settled`` yet. Run the same statement
    against a live database and it would silently discard any legitimate
    ledger activity recorded since on an order that also happens to carry an
    old legacy payment -- overwriting a correct, larger ``amount_settled``
    back down to just the legacy row's own total. This script instead scopes
    every settlement write to one order, inside a lock, conditional on
    ``amount_settled`` still being exactly 0 (see below for why that is a
    provable precondition, not an assumption).
  - Its ``INSERT ... SELECT ... WHERE NOT EXISTS`` pattern has no row lock:
    two concurrent runs, or one run racing a live payment, could both pass
    the ``NOT EXISTS`` check before either commits.

Why "legacy payment, no ledger payment entry" implies ``amount_settled == 0``
------------------------------------------------------------------------------
Nothing in this codebase writes to ``orders.amount_settled`` except (a) the
M7.5.1 migration's one-time backfill, which never touches an order without
also creating its ledger payment entry in the same breath, and (b)
``app/services/payment_ledger.py``, which never touches ``amount_settled``
without also creating a ledger entry. So an order with a legacy payment and
*no* ledger payment entry has necessarily never had its ``amount_settled``
touched by anything -- it must still hold the column's own default, 0. This
script re-verifies that precondition under lock before writing anything, and
treats a violation (found non-zero) as a sign something unexpected happened
-- flagged for manual review, never guessed at or overwritten.

Safety model
------------
  - **Dry run by default.** No write happens unless ``--apply`` is given.
  - ``--apply`` also requires ``--confirm "RECONCILE <n>"``, where ``<n>`` is
    the exact eligible count *this run's own scan* just printed -- typed by
    an operator who has read the dry-run report above it, not copy-pasted
    from a previous run or another script (mirrors
    ``scripts/restore_database.py``'s ``--confirm "RESTORE <target>"``).
  - A non-SQLite target database name must end in ``_test`` unless
    ``--allow-production-target`` is also given (same convention as
    ``scripts/restore_database.py``; see ``scripts/_backup_common.py``).
  - **One order per transaction.** Each eligible order is locked with
    ``SELECT ... FOR UPDATE`` (the same helper shape as
    ``app/services/payment_ledger.py``'s ``_lock_order``), then the legacy
    payment row itself is locked and re-fetched the same way -- eligibility
    is re-checked *inside* both locks immediately before writing, against
    values read only from those locked copies, never from this run's own
    earlier scan -- closing the gap between the dry-run scan and the write
    for both the order and the payment it is backfilled from. The
    settlement update is a conditional, SQL-computed
    ``UPDATE ... WHERE amount_settled = 0``, never an unconditional
    recomputation. A failure or a detected conflict on one order rolls back
    only that order's transaction and is reported; it never aborts the run
    or silently skips without being counted.
  - A record that does not cleanly fit -- an order with *both* a legacy
    payment and an existing ledger entry, a legacy row whose own data looks
    inconsistent, or one whose ``amount_settled`` is unexpectedly non-zero --
    is reported for manual review and left completely untouched.
  - No audit_logs row is written (matching the original migration's own
    backfill, which never wrote one either -- adding a new audit event type
    would need its own migration, out of this script's scope). The ledger
    entries themselves, each carrying a ``reason`` naming this script, are
    the durable record; this run's own report is the operator-facing one.

See docs/PRODUCTION_READINESS.md for the tracked production-readiness item
and the operator runbook this script implements.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@dataclass
class OrderOutcome:
    order_id: int | None
    action: str  # "reconciled" | "flagged" | "failed"
    detail: str


@dataclass
class ReconciliationReport:
    outcomes: list[OrderOutcome] = field(default_factory=list)
    already_reconciled_count: int = 0
    eligible_total: int = 0

    def add(self, order_id: int | None, action: str, detail: str) -> None:
        self.outcomes.append(OrderOutcome(order_id, action, detail))

    def by_action(self, action: str) -> list[OrderOutcome]:
        return [o for o in self.outcomes if o.action == action]


def _ineligibility_reason(payment) -> str | None:
    """Defensive checks a legacy row should never fail given its own CHECK
    constraints (``ck_payments_amount_positive``,
    ``ck_payments_refund_within_amount``) -- kept anyway so a future schema
    change or an already-corrupted row is reported, not trusted blindly."""
    if payment.recorded_by_id is None:
        return "legacy payment has no recorded_by_id (payment_ledger_entries requires one for 'payment')"
    if payment.refunded_amount < 0 or payment.refunded_amount > payment.amount:
        return f"legacy refunded_amount ({payment.refunded_amount}) is out of range for amount ({payment.amount})"
    return None


@dataclass
class ScanResult:
    clean: list  # list[Payment] -- eligible for reconciliation
    flagged: list  # list[tuple[Payment, str]] -- reported, never touched
    already_reconciled_count: int


def scan(limit: int | None = None) -> ScanResult:
    """Read-only. Finds every legacy ``payments`` row with no corresponding
    ledger 'payment' entry, classifies each as eligible or flagged, and
    separately counts (without enumerating) orders that already have both --
    the expected, common state for every order that predates the M7.5.1
    migration, not itself a sign of trouble."""
    import sqlalchemy as sa

    from app.extensions import db
    from app.models.order import Order
    from app.models.payment import Payment
    from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry

    ledger_payment_order_ids = sa.select(PaymentLedgerEntry.order_id).where(
        PaymentLedgerEntry.entry_type == LedgerEntryType.PAYMENT
    )

    already_reconciled_count = db.session.execute(
        sa.select(sa.func.count(Payment.id)).where(Payment.order_id.in_(ledger_payment_order_ids))
    ).scalar_one()

    query = (
        sa.select(Payment, Order.amount_settled)
        .join(Order, Order.id == Payment.order_id)
        .where(~Payment.order_id.in_(ledger_payment_order_ids))
        .order_by(Payment.order_id)
    )
    if limit is not None:
        query = query.limit(limit)

    clean: list = []
    flagged: list = []
    for payment, amount_settled in db.session.execute(query).all():
        if amount_settled != 0:
            flagged.append((payment, f"amount_settled is {amount_settled}, expected 0 for an order with no "
                                      "ledger payment entry yet -- not a staleness assumption, a real precondition "
                                      "violation (see module docstring)"))
            continue
        reason = _ineligibility_reason(payment)
        if reason:
            flagged.append((payment, reason))
        else:
            clean.append(payment)

    return ScanResult(clean=clean, flagged=flagged, already_reconciled_count=already_reconciled_count)


def _lock_order_for_update(order_id: int):
    """Same shape as app/services/payment_ledger.py's _lock_order -- kept
    local rather than imported since that name is private to its module."""
    import sqlalchemy as sa

    from app.extensions import db
    from app.models.order import Order

    return db.session.execute(
        sa.select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def _lock_payment_for_update(payment_id: int, order_id: int):
    """Locks the legacy ``payments`` row itself -- same shape and the same
    reason as ``_lock_order_for_update``. ``populate_existing=True`` forces
    this read to overwrite any copy of this exact row already sitting in
    the session's identity map (this run's own earlier ``scan()`` loaded
    one for every clean candidate) with whatever is current under the lock,
    rather than silently reusing the cached, possibly stale, attributes a
    plain ``Session.get()`` would hand back unchanged.

    Scoped to ``order_id`` too (``order_id`` is immutable once a payment is
    created, so this can never legitimately fail) purely as a second,
    defensive check against ever acting on the wrong payment row -- the
    same belt-and-braces style as ``get_payment_entry_for_order`` in
    app/services/payment_ledger.py.

    Returns None if the row no longer exists; the caller must treat that
    conservatively (flag and skip), never by falling back to any
    pre-lock copy of it."""
    import sqlalchemy as sa

    from app.extensions import db
    from app.models.payment import Payment

    return db.session.execute(
        sa.select(Payment).where(Payment.id == payment_id, Payment.order_id == order_id)
        .with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _apply_one(payment_id: int) -> OrderOutcome:
    """Re-locks the order, then the legacy payment row, and re-verifies
    eligibility from scratch against both -- this run's own earlier scan is
    advisory only, never trusted at write time.

    Locking order: the order row is locked first, exactly as every other
    writer in this codebase locks it first (app/services/payment_ledger.py,
    app/services/pricing.py's apply_discount) -- so this introduces no new
    lock-acquisition order and cannot deadlock against any of them. The
    legacy payment row is locked second, only once the order lock is
    already held; nothing else in this codebase ever takes an explicit
    lock on a payments row at all (app/services/payments.py, whose retired
    refund_payment's conditional UPDATE used to acquire its row lock
    implicitly, scoped to that one statement, never nested with an order
    lock before or after it, was removed outright in M7.5.4.7 -- one fewer
    writer to reason about, not a new one), so there is no existing code
    path that could lock a payment row and then an order row, which is the
    only ordering that could deadlock against this one.

    Both locked reads use populate_existing=True, so every field this
    function uses below (the order's amount_settled, and the payment's
    amount/refunded_amount/method/recorded_by_id/refund_reason/refunded_at)
    comes only from that fresh, locked copy -- never from a value this
    session happened to already hold from this run's own earlier scan().

    Catches any exception, not only SQLAlchemyError: one order's failure
    (database or otherwise) must never abort the run or stop other eligible
    orders from being attempted and reported (see module docstring)."""
    import sqlalchemy as sa

    from app.extensions import db
    from app.models.order import Order
    from app.models.payment import Payment
    from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
    from app.services.audit import commit_with

    order_id = None
    try:
        # A bare, unlocked lookup of the immutable FK only -- never used for
        # anything that could be stale (order_id cannot change after a
        # payment is created), just to know which order to lock first.
        order_id = db.session.execute(
            sa.select(Payment.order_id).where(Payment.id == payment_id)
        ).scalar_one_or_none()
        if order_id is None:
            return OrderOutcome(None, "failed", f"legacy payment id={payment_id} no longer exists")

        locked = _lock_order_for_update(order_id)

        existing = db.session.execute(
            sa.select(PaymentLedgerEntry.id)
            .where(PaymentLedgerEntry.order_id == order_id, PaymentLedgerEntry.entry_type == LedgerEntryType.PAYMENT)
        ).first()
        if existing is not None:
            db.session.rollback()
            return OrderOutcome(order_id, "flagged",
                                 "a ledger payment entry appeared on this order after the scan; "
                                 "skipped, nothing written")
        if locked.amount_settled != 0:
            db.session.rollback()
            return OrderOutcome(order_id, "flagged",
                                 f"amount_settled changed to {locked.amount_settled} after the scan; "
                                 "skipped, nothing written")

        # The legacy payment row itself: re-fetched and locked here, not
        # trusted from this run's own earlier scan() or from the bare
        # order_id lookup above (see this function's docstring).
        payment = _lock_payment_for_update(payment_id, order_id)
        if payment is None:
            db.session.rollback()
            return OrderOutcome(order_id, "flagged",
                                 "the legacy payment row disappeared before the locked recheck; "
                                 "skipped, nothing written")
        reason = _ineligibility_reason(payment)
        if reason:
            db.session.rollback()
            return OrderOutcome(order_id, "flagged", reason)

        entry = PaymentLedgerEntry(
            order_id=order_id, entry_type=LedgerEntryType.PAYMENT, amount=payment.amount, method=payment.method,
            refunded_amount=payment.refunded_amount, corrected_amount=Decimal("0.00"),
            recorded_by_id=payment.recorded_by_id, created_at=payment.captured_at,
            reason=f"Backfilled by scripts/reconcile_payment_ledger.py from legacy payments.id={payment.id}",
        )
        db.session.add(entry)
        db.session.flush()  # entry.id needed below before the refund row can reference it

        if payment.refunded_amount > 0:
            db.session.add(PaymentLedgerEntry(
                order_id=order_id, entry_type=LedgerEntryType.REFUND, amount=-payment.refunded_amount,
                reversed_entry_id=entry.id, reason=payment.refund_reason, recorded_by_id=None,
                created_at=payment.refunded_at or payment.captured_at,
            ))

        settlement = payment.amount - payment.refunded_amount
        result = db.session.execute(
            sa.update(Order).where(Order.id == order_id, Order.amount_settled == 0)
            .values(amount_settled=settlement).execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.session.rollback()
            return OrderOutcome(order_id, "flagged",
                                 "amount_settled was no longer 0 at write time; skipped, nothing written")

        commit_with()
        return OrderOutcome(order_id, "reconciled",
                             f"ledger payment entry id={entry.id}, amount_settled set to {settlement}")
    except Exception as exc:
        db.session.rollback()
        return OrderOutcome(order_id, "failed", f"{exc.__class__.__name__}: {exc}")


def build_dry_run_report(result: ScanResult) -> ReconciliationReport:
    report = ReconciliationReport(already_reconciled_count=result.already_reconciled_count,
                                   eligible_total=len(result.clean))
    for payment in result.clean:
        report.add(payment.order_id, "eligible",
                   f"legacy amount={payment.amount} method={payment.method.value} "
                   f"refunded_amount={payment.refunded_amount} recorded_by_id={payment.recorded_by_id}")
    for payment, reason in result.flagged:
        report.add(payment.order_id, "flagged", reason)
    return report


def apply_reconciliation(result: ScanResult, limit: int | None = None) -> ReconciliationReport:
    """Applies against exactly the candidates ``result.clean`` names -- each
    one is independently re-verified under its own lock in _apply_one, so
    reusing this list (rather than re-scanning) does not weaken safety."""
    report = ReconciliationReport(already_reconciled_count=result.already_reconciled_count,
                                   eligible_total=len(result.clean))
    targets = result.clean if limit is None else result.clean[:limit]
    for payment in targets:
        outcome = _apply_one(payment.id)
        report.outcomes.append(outcome)
    for payment, reason in result.flagged:
        report.add(payment.order_id, "flagged", reason)
    return report


def _print_report(report: ReconciliationReport, *, applied: bool) -> None:
    from scripts._backup_common import ok, say, step, warn

    step("Already reconciled (legacy payment + ledger entry both present -- not touched)")
    say(f"    {report.already_reconciled_count} order(s)")

    action = "reconciled" if applied else "eligible"
    step(f"{'Reconciled' if applied else 'Eligible for reconciliation'}")
    rows = report.by_action(action)
    if not rows:
        say("    none")
    for row in rows:
        (ok if applied else say)(f"    order #{row.order_id}: {row.detail}")

    flagged = report.by_action("flagged")
    step("Flagged for manual review (not modified)")
    if not flagged:
        say("    none")
    for row in flagged:
        warn(f"order #{row.order_id}: {row.detail}")

    failed = report.by_action("failed")
    if failed:
        step("Failed (not applied, rolled back)")
        for row in failed:
            warn(f"order #{row.order_id}: {row.detail}")


def main(argv: list[str] | None = None) -> int:
    from scripts._backup_common import BackupError, is_disposable_test_name, say, step, validate_database_name

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--apply", action="store_true", help="write changes; omitted means a read-only dry run")
    parser.add_argument("--confirm", default=None, metavar='"RECONCILE <n>"',
                         help="required with --apply: exactly 'RECONCILE <n>', where <n> is the eligible count "
                              "this run's own dry-run report prints -- not copy-pasted from an earlier run")
    parser.add_argument("--allow-production-target", action="store_true",
                         help="required (for a non-SQLite --database-url) if the database name does not end "
                              "in '_test'")
    parser.add_argument("--limit", type=int, default=None, help="process at most this many eligible orders")
    args = parser.parse_args(argv)

    if not args.database_url:
        say("No DATABASE_URL and no --database-url given.")
        return 2

    # Must happen before `from app import create_app` below: BaseConfig reads
    # DATABASE_URL from the environment at class-definition time (the first
    # import of the `config` module), the same binding-time rule
    # tests/test_payment_ledger_migration.py's _app() works around.
    os.environ["DATABASE_URL"] = args.database_url

    from sqlalchemy.engine import make_url
    url = make_url(args.database_url)
    if url.get_backend_name() != "sqlite":
        name = url.database or ""
        try:
            validate_database_name(name)
        except BackupError as exc:
            say(f"[FAILED] {exc}")
            return 1
        if not is_disposable_test_name(name) and not args.allow_production_target:
            say(f"[FAILED] {name!r} does not end in '_test'. Reconciling it requires --allow-production-target.")
            return 1

    from app import create_app
    app = create_app()
    with app.app_context():
        result = scan(limit=args.limit)
        dry_report = build_dry_run_report(result)
        _print_report(dry_report, applied=False)

        eligible = len(result.clean)
        if not args.apply:
            say("")
            say("Dry run only -- nothing was written. Pass --apply --confirm \"RECONCILE "
                f"{eligible}\" to apply.")
            return 0

        expected_confirm = f"RECONCILE {eligible}"
        if args.confirm != expected_confirm:
            say("")
            say(f"[FAILED] --confirm did not match exactly. Expected {expected_confirm!r}, got {args.confirm!r}.")
            say("Nothing was written. Re-read the dry-run report above and pass that exact phrase to apply.")
            return 1

        if eligible == 0:
            say("")
            say("Nothing eligible to reconcile.")
            return 0

        step(f"Applying -- reconciling up to {eligible} order(s)")
        apply_report = apply_reconciliation(result, limit=args.limit)
        _print_report(apply_report, applied=True)
        return 0 if not apply_report.by_action("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
