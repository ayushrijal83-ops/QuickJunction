"""Milestone 7.5.4.8: CLI-level safety coverage for
scripts/reconcile_payment_ledger.py's ``main()`` entry point.

tests/test_payment_ledger_reconciliation.py already exercises scan() and
apply_reconciliation() directly against the standard db fixture -- the
reconciliation algorithm itself is proven there and is not retested here.
This file exercises the real ``main()`` instead: argument parsing, the
non-SQLite target gate, the ``--confirm`` phrase, and the dry-run/--apply
split -- the layer that decides whether any of that algorithm gets to run
against a given database at all.

Calling main() directly in-process, not via subprocess
--------------------------------------------------------------------------
``main()`` does ``os.environ["DATABASE_URL"] = args.database_url`` before
``from app import create_app`` specifically because ``config.BaseConfig``
reads ``DATABASE_URL`` at class-definition time -- the *first* import of the
``config`` module (see that line's own comment, and
tests/test_payment_ledger_migration.py's ``_app()`` helper, which hits the
identical problem). In this test process ``config``/``app`` are already
imported (by conftest.py's own ``from app import create_app``, before any
test runs), so that binding has already happened and cannot be redone by
mutating the environment a second time. The fix used here is the same shape
as every other place in this codebase that calls scripts/*.py code expecting
a fresh process's worth of environment binding: swap out the one thing that
actually depends on it. ``main()``'s own ``from app import create_app`` is a
local import inside the function body, re-executed on every call, so
monkeypatching ``app.create_app`` to return the test ``app`` fixture's
already-built Flask app (bound to the standard in-memory SQLite test
database) is picked up transparently -- every other line of ``main()`` runs
completely unmodified and for real: real argparse, real URL parsing, real
name/target validation, real confirm-phrase check, real scan() and
apply_reconciliation() calls, against a real (if ephemeral) database.

test_non_test_mysql_target_is_refused_without_ever_building_an_app and its
sibling gate tests are what actually prove the gate-ordering claim: each
makes create_app() raise if it is ever called at all, which is airtight
regardless of what a connection attempt would otherwise do.
test_true_subprocess_mysql_target_is_refused_before_any_connection_attempt
is a different, complementary kind of check -- a genuine fresh subprocess
(python scripts/reconcile_payment_ledger.py ...) pointed at an unreachable
MySQL-shaped URL, the real command an operator would type, with no
monkeypatching at all. It confirms the real CLI's *current* behavior end to
end (clean, fast refusal; no hang; no crash) -- see its own docstring for
why that is not, by itself, a second independent proof of ordering.

Boundary respected throughout: no test here ever opens a real MySQL
connection, let alone one pointed at an installation database. Every
"mysql+pymysql://..." URL used below is either rejected before a connection
would be attempted (the gate tests) or is routed, via the create_app()
monkeypatch, straight to the same in-memory SQLite test database every other
test in this suite uses -- the string is exercised only as a string.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import app as app_pkg
import scripts.reconcile_payment_ledger as reconcile_module
from app.extensions import db as _db
from app.models.order import Order
from app.models.payment_ledger import PaymentLedgerEntry
from tests.conftest import make_legacy_payment
from tests.test_payments import member
from tests.test_restaurant_ops import customer, place

ROOT = Path(__file__).resolve().parent.parent
D = Decimal
_seq = itertools.count()


def _legacy_paid_order(price="100.00"):
    """One order in the exact pre-cutover shape the reconciler exists to
    repair: a legacy payments row, no ledger entry, amount_settled still 0.
    Mirrors test_payment_ledger_reconciliation.py's own helper; duplicated
    rather than imported so this file's fixtures do not depend on that
    file's internals.

    customer()/member() each default to a fixed username ("diner"/"cook"),
    which collides on a UNIQUE constraint the moment a test seeds more than
    one order -- every call here gets its own suffix so tests seeding two or
    three orders (the stale-confirm-count and the one-order-fails cases
    below) don't crash on user creation before reconciliation is even
    reached."""
    n = next(_seq)
    order = place(customer(username=f"diner{n}"), price=price, qty=1)
    payment = make_legacy_payment(order, member(name=f"cook{n}"))
    _db.session.refresh(order)
    assert order.amount_settled == D("0.00")
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 0
    return order, payment


def _assert_nothing_written(order_id: int) -> None:
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order_id).count() == 0
    assert _db.session.get(Order, order_id).amount_settled == D("0.00")


def _run_main(argv: list[str]) -> int:
    """main() pushes its own nested Flask app context (``with
    app.app_context():``). Flask-SQLAlchemy 3.x scopes ``db.session`` by
    app-context id (``flask_sqlalchemy.extension._make_scoped_session``'s
    default ``scopefunc``), so even though the faked create_app() returns
    this *same* Flask app object, main()'s inner context gets its own,
    separate session -- whatever it commits is invisible to this test's own
    session (and the ``order``/``payment`` objects already cached in it)
    until that session is told to stop trusting its cache. expire_all()
    does that: every already-loaded object is re-read from the database on
    its next attribute access instead of silently returning its
    pre-reconciliation copy. This matters as much for proving a refusal
    path really wrote nothing as it does for checking a successful apply's
    effects -- without it, a stale cache could make either kind of
    assertion pass for the wrong reason.

    Guarded by has_app_context(): the gate-refusal tests call this with no
    ``app``/``db`` fixture active at all (main() never gets far enough to
    build one), so there is no test-side session to expire."""
    from flask import has_app_context

    result = reconcile_module.main(argv)
    if has_app_context():
        _db.session.expire_all()
    return result


def _fake_create_app(monkeypatch, application):
    """main()'s ``from app import create_app`` is a local import, re-resolved
    against ``app.create_app`` on every call -- patching that attribute is
    enough, no need to touch ``scripts.reconcile_payment_ledger`` itself."""
    monkeypatch.setattr(app_pkg, "create_app", lambda *a, **kw: application)


@pytest.fixture(autouse=True)
def _own_database_url_env(monkeypatch):
    """main() writes ``os.environ["DATABASE_URL"]`` directly (see module
    docstring above). monkeypatch only restores a variable at teardown if it
    is the one that changed it first -- seed it here so every test's write to
    that key, however it happens, is cleaned up afterward regardless of
    which test method does the writing."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused-placeholder")


# =============================================================================
# 1. dry-run is the default
# =============================================================================


def test_dry_run_is_default_and_writes_nothing(app, db, monkeypatch, capsys):
    order, _ = _legacy_paid_order(price="40.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main(["--database-url", "sqlite:///irrelevant-app-is-faked.db"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry run only" in out
    assert "Eligible for reconciliation" in out
    _assert_nothing_written(order.id)


# =============================================================================
# 2 & 3. --apply without, or with the wrong, --confirm is refused
# =============================================================================


def test_apply_without_confirm_is_refused(app, db, monkeypatch, capsys):
    order, _ = _legacy_paid_order(price="25.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main(["--database-url", "sqlite:///irrelevant.db", "--apply"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "did not match exactly" in out
    assert "Nothing was written" in out
    _assert_nothing_written(order.id)


def test_apply_with_wrong_confirm_count_is_refused(app, db, monkeypatch, capsys):
    order, _ = _legacy_paid_order(price="25.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "sqlite:///irrelevant.db", "--apply", "--confirm", "RECONCILE 999",
    ])

    assert rc == 1
    out = capsys.readouterr().out
    assert "did not match exactly" in out
    assert "Expected 'RECONCILE 1'" in out
    _assert_nothing_written(order.id)


def test_apply_with_confirm_copied_from_an_earlier_run_is_refused(app, db, monkeypatch, capsys):
    """The confirm phrase must come from *this* run's own dry-run count, not
    a number that happened to be correct for a previous, smaller run -- two
    eligible orders exist now, so last run's "RECONCILE 1" is stale."""
    order1, _ = _legacy_paid_order(price="10.00")
    order2, _ = _legacy_paid_order(price="20.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "sqlite:///irrelevant.db", "--apply", "--confirm", "RECONCILE 1",
    ])

    assert rc == 1
    _assert_nothing_written(order1.id)
    _assert_nothing_written(order2.id)


# =============================================================================
# 4. correct confirmation applies
# =============================================================================


def test_apply_with_correct_confirm_reconciles(app, db, monkeypatch, capsys):
    order, payment = _legacy_paid_order(price="55.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "sqlite:///irrelevant.db", "--apply", "--confirm", "RECONCILE 1",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"order #{order.id}" in out
    entry = _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).one()
    assert entry.amount == payment.amount
    assert _db.session.get(Order, order.id).amount_settled == payment.amount


# =============================================================================
# 5. non-test MySQL targets are refused before any database connection
# =============================================================================


def test_non_test_mysql_target_is_refused_without_ever_building_an_app(monkeypatch, capsys):
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("create_app() must not run before the target gate decides")

    monkeypatch.setattr(app_pkg, "create_app", _must_not_be_called)

    rc = _run_main([
        "--database-url", "mysql+pymysql://user:pw@127.0.0.1:1/quick_junction",
    ])

    assert rc == 1
    out = capsys.readouterr().out
    assert "does not end in '_test'" in out
    assert "--allow-production-target" in out


def test_mysql_system_schema_target_is_refused_without_ever_building_an_app(monkeypatch, capsys):
    """validate_database_name()'s system-schema check runs in the same spot,
    for the same reason -- also proven never to reach create_app()."""
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("create_app() must not run before the target gate decides")

    monkeypatch.setattr(app_pkg, "create_app", _must_not_be_called)

    rc = _run_main([
        "--database-url", "mysql+pymysql://user:pw@127.0.0.1:1/mysql", "--allow-production-target",
    ])

    assert rc == 1
    out = capsys.readouterr().out
    assert "system schema" in out


def test_true_subprocess_mysql_target_is_refused_before_any_connection_attempt():
    """The airtight proof that the gate never reaches create_app() is the
    monkeypatched tests above (test_non_test_mysql_target_is_refused_without_
    ever_building_an_app and friends): they make create_app() raise if it is
    ever called at all, regardless of what any connection attempt would do.
    This test does not re-establish that ordering guarantee by itself -- a
    closed local port such as 127.0.0.1:1 typically returns ECONNREFUSED
    near-instantly rather than hanging, so a hypothetical regression that
    attempted a connection *before* the gate could still fail fast, be
    caught, and fall through to an identical-looking refusal message well
    within this test's timeout. (The "Traceback" in stderr assertion below
    narrows that gap somewhat -- an uncaught connection error would print
    one -- but does not close it entirely, since a regression could also
    catch and suppress it.)

    What this test genuinely adds: it runs the real, published command
    (``python scripts/reconcile_payment_ledger.py --database-url ...``) in a
    brand-new process with no monkeypatching of any kind, pointed at a host
    nothing listens on, and confirms it is the actual *current* behavior --
    a clean, fast refusal with the expected message, not a hang and not a
    crash. That is a meaningful end-to-end sanity check on its own; it is
    not, by itself, proof of gate ordering."""
    # Matches tests/test_privilege_separation.py's own subprocess tests: a
    # copy of this process's real environment (Windows subprocess startup is
    # unreliable with a hand-built minimal env -- SYSTEMROOT/PATH/TEMP all
    # matter), with only the specific keys this run cares about overridden.
    env = dict(os.environ)
    env["APP_ENV"] = "development"
    env["SECRET_KEY"] = "x" * 64
    env["LLM_WARMUP"] = "false"
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        [sys.executable, "scripts/reconcile_payment_ledger.py",
         "--database-url", "mysql+pymysql://user:pw@127.0.0.1:1/quick_junction"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 1
    assert "does not end in '_test'" in result.stdout
    assert "Traceback" not in result.stderr


# =============================================================================
# 6. the production-target override does not bypass --confirm
# =============================================================================


def test_production_override_without_confirm_is_still_refused(app, db, monkeypatch, capsys):
    order, _ = _legacy_paid_order(price="15.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "mysql+pymysql://user:pw@127.0.0.1/quick_junction",
        "--allow-production-target", "--apply",
    ])

    assert rc == 1
    out = capsys.readouterr().out
    # Refused for the confirm reason, not the target-name reason -- proving
    # the override did its one job (clearing the name gate) and nothing more.
    assert "does not end in '_test'" not in out
    assert "did not match exactly" in out
    _assert_nothing_written(order.id)


def test_production_override_with_wrong_confirm_is_still_refused(app, db, monkeypatch, capsys):
    order, _ = _legacy_paid_order(price="15.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "mysql+pymysql://user:pw@127.0.0.1/quick_junction",
        "--allow-production-target", "--apply", "--confirm", "RECONCILE 999",
    ])

    assert rc == 1
    _assert_nothing_written(order.id)


def test_production_override_with_correct_confirm_applies(app, db, monkeypatch, capsys):
    """The override's one job is clearing the name gate -- with a correct
    confirm phrase too, the run proceeds exactly as a `_test`-suffixed
    target would. (The "mysql+pymysql://..." URL string is only ever
    parsed for its database name here -- create_app() is faked to the
    in-memory SQLite test app, so no MySQL connection is attempted.)"""
    order, payment = _legacy_paid_order(price="15.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "mysql+pymysql://user:pw@127.0.0.1/quick_junction",
        "--allow-production-target", "--apply", "--confirm", "RECONCILE 1",
    ])

    assert rc == 0
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order.id).count() == 1
    assert _db.session.get(Order, order.id).amount_settled == payment.amount


# =============================================================================
# 7. malformed or unsupported database URLs are rejected safely
# =============================================================================


def test_malformed_database_url_is_rejected_safely(monkeypatch, capsys):
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("create_app() must not run for an unparsable --database-url")

    monkeypatch.setattr(app_pkg, "create_app", _must_not_be_called)

    rc = _run_main(["--database-url", "not a url at all"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAILED]" in out
    assert "not a valid database URL" in out
    assert "not a url at all" not in out  # never echoed back


def test_database_url_with_a_non_numeric_port_is_rejected_safely(monkeypatch, capsys):
    """make_url() raises ValueError (not ArgumentError) for this one --
    config.py's own _parse_database_url catches both for the same reason;
    main() must too."""
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("create_app() must not run for an unparsable --database-url")

    monkeypatch.setattr(app_pkg, "create_app", _must_not_be_called)

    rc = _run_main([
        "--database-url", "mysql+pymysql://user:supersecret@host:notaport/quick_junction_test",
    ])

    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAILED]" in out
    assert "supersecret" not in out  # credential never echoed back


def test_empty_database_url_is_the_missing_argument_case(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    rc = _run_main([])

    assert rc == 2
    assert "No DATABASE_URL" in capsys.readouterr().out


# =============================================================================
# 8. invalid argument combinations are rejected
# =============================================================================


def test_unknown_flag_is_rejected_by_argparse(monkeypatch):
    monkeypatch.setattr(app_pkg, "create_app",
                         lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not reach create_app")))
    with pytest.raises(SystemExit) as exc_info:
        reconcile_module.main(["--database-url", "sqlite:///x.db", "--not-a-real-flag"])
    assert exc_info.value.code == 2


def test_disallowed_target_takes_priority_over_an_already_correct_confirm(app, db, monkeypatch, capsys):
    """--apply --confirm "RECONCILE <n>" together with a disallowed
    production target (no --allow-production-target) must still be refused
    for the target reason -- the name gate runs, and must keep running,
    before create_app()/scan() exist to produce a count at all."""
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("create_app() must not run before the target gate decides")

    monkeypatch.setattr(app_pkg, "create_app", _must_not_be_called)

    rc = _run_main([
        "--database-url", "mysql+pymysql://user:pw@127.0.0.1/quick_junction",
        "--apply", "--confirm", "RECONCILE 1",
    ])

    assert rc == 1
    assert "does not end in '_test'" in capsys.readouterr().out


def test_limit_zero_with_apply_writes_nothing(app, db, monkeypatch, capsys):
    """--limit applies to scan() itself (``scan(limit=args.limit)``), not
    only to which of an already-fully-scanned clean list gets applied -- so
    --limit 0 makes the scan itself report zero eligible orders, and the
    confirm phrase must match that (0), not the order's true, unscanned
    eligibility. Nothing gets written either way; the order is left exactly
    as untouched as if it had never been scanned at all -- not even flagged."""
    order, _ = _legacy_paid_order(price="30.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "sqlite:///irrelevant.db", "--apply", "--confirm", "RECONCILE 0", "--limit", "0",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing eligible to reconcile." in out
    _assert_nothing_written(order.id)


def test_limit_restricts_the_scan_itself_not_just_the_apply_step(app, db, monkeypatch, capsys):
    """Two genuinely eligible orders exist; --limit 1 must cap scan() at one
    of them -- the other is left completely alone this run, not merely
    unapplied: not reconciled, not flagged, not even counted as eligible in
    the report. (scan() orders by Payment.order_id, so order_a, created
    first, is the one --limit 1 keeps.)"""
    order_a, payment_a = _legacy_paid_order(price="11.00")
    order_b, _ = _legacy_paid_order(price="22.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "sqlite:///irrelevant.db", "--apply", "--confirm", "RECONCILE 1", "--limit", "1",
    ])

    assert rc == 0
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=order_a.id).count() == 1
    assert _db.session.get(Order, order_a.id).amount_settled == payment_a.amount
    _assert_nothing_written(order_b.id)


# =============================================================================
# 9. SQLite test-database behavior remains correct
# =============================================================================


def test_sqlite_target_skips_the_production_name_gate_entirely(app, db, monkeypatch, capsys):
    """The `_test`-suffix/system-schema gate only applies `if
    url.get_backend_name() != "sqlite"` -- a sqlite target with a
    production-sounding file name still needs no --allow-production-target.
    This is the one case where that is correct: scripts/reconcile_payment_ledger.py's
    own module docstring and docs/PRODUCTION_READINESS.md both describe the
    gate as a MySQL-target safeguard; every real SQLite file used against
    this script is a disposable, locally-created one by construction."""
    order, payment = _legacy_paid_order(price="12.00")
    _fake_create_app(monkeypatch, app)

    rc = _run_main([
        "--database-url", "sqlite:////tmp/quick_junction_production.db",
        "--apply", "--confirm", "RECONCILE 1",
    ])

    assert rc == 0
    assert _db.session.get(Order, order.id).amount_settled == payment.amount


# =============================================================================
# 10. refusal paths never create ledger entries or modify settlement
#     projections -- already asserted inline in every refusal test above via
#     _assert_nothing_written(); this test adds one more angle: a failure
#     *during* apply (one order's lock raising) must not leak a partial
#     write for that order, while not aborting the whole run either.
# =============================================================================


def test_one_order_failing_during_apply_leaves_it_untouched(app, db, monkeypatch, capsys):
    good_order, _ = _legacy_paid_order(price="10.00")
    bad_order, _ = _legacy_paid_order(price="20.00")
    _fake_create_app(monkeypatch, app)

    real_lock = reconcile_module._lock_order_for_update

    def flaky_lock(order_id):
        if order_id == bad_order.id:
            raise RuntimeError("simulated lock failure")
        return real_lock(order_id)

    monkeypatch.setattr(reconcile_module, "_lock_order_for_update", flaky_lock)

    rc = _run_main([
        "--database-url", "sqlite:///irrelevant.db", "--apply", "--confirm", "RECONCILE 2",
    ])

    assert rc == 1  # a failed order makes main() report failure
    assert _db.session.query(PaymentLedgerEntry).filter_by(order_id=good_order.id).count() == 1
    _assert_nothing_written(bad_order.id)
