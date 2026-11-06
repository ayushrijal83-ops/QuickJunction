"""Milestone 10 verification: the two M10 races on a real MySQL server.

Runs only when ``TEST_DATABASE_URL`` points at MySQL (it is skipped on the
default in-memory SQLite, where the stale-read race tests in
test_restaurant_ops.py cover the logic). Each round starts two threads, each
with its own application context, session and pooled MySQL connection, lines
them up on a barrier and fires both writes at once -- InnoDB row locks and the
unique index are what decide the winner, not the test.

The verifying reads end the main session's transaction first: under MySQL's
default REPEATABLE READ, a transaction that has already read keeps its
snapshot, so ``expire_all()`` alone would re-read pre-race data. The
application is not exposed to this -- its conditional UPDATE and the unique
index use InnoDB's locking current read, and it commits before refreshing.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest

from app.extensions import db as _db
from app.models.order import Order, OrderStatus
from app.models.reservation import Reservation
from app.models.user import Role, User
from app.services.orders import OrderStatusError, cancel_order_as_customer, update_order_status
from app.services.reservations import ReservationError, create_reservation
from app.services.tables import create_table
from app.utils.clock import db_today
from tests.conftest import make_user

ROUNDS = 15


@pytest.fixture(autouse=True)
def _mysql_only(app):
    if _db.engine.dialect.name != "mysql":
        pytest.skip("real-concurrency verification needs TEST_DATABASE_URL=mysql+pymysql://...")


def _race(app, *actions):
    """Run each action(tag) in its own thread + app context at the same instant."""
    barrier, results = threading.Barrier(len(actions)), []

    def run(action):
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                results.append(action())
            except Exception as exc:  # recorded, asserted on by the caller
                results.append(exc)
            finally:
                _db.session.remove()

    threads = [threading.Thread(target=run, args=(a,)) for a in actions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_customer_cancel_vs_start_preparing_exactly_one_wins(app, db):
    from tests.test_restaurant_ops import place

    customer = make_user(username="racecust", email="rc@example.com")
    # Plain ints for the worker threads: touching an ORM object owned by this
    # thread's session from another thread would share its connection.
    customer_id = customer.id
    cook_id = make_user(username="racecook", email="rk@example.com", role=Role.STAFF).id
    outcomes = {"cancel": 0, "prepare": 0}

    for _ in range(ROUNDS):
        order = place(customer)
        order.status = OrderStatus.CONFIRMED
        _db.session.commit()
        order_id = order.id

        def cancel():
            o, u = _db.session.get(Order, order_id), _db.session.get(User, customer_id)
            try:
                cancel_order_as_customer(o, u, "race")
                return "cancel"
            except OrderStatusError:
                return "rejected"

        def prepare():
            o, s = _db.session.get(Order, order_id), _db.session.get(User, cook_id)
            try:
                update_order_status(o, OrderStatus.PREPARING, actor=s)
                return "prepare"
            except OrderStatusError:
                return "rejected"

        results = _race(app, cancel, prepare)
        assert sorted(map(str, results)) in (["cancel", "rejected"], ["prepare", "rejected"]), results
        winner = next(r for r in results if r != "rejected")
        outcomes[winner] += 1

        _db.session.rollback()  # end this session's REPEATABLE-READ snapshot (see module docstring)
        row = _db.session.get(Order, order_id)
        if winner == "cancel":
            assert row.status == OrderStatus.CANCELLED and row.cancelled_at and row.cancelled_by_id == customer_id
            assert row.preparing_at is None  # M14: the losing kitchen action stamped nothing
        else:
            assert row.status == OrderStatus.PREPARING
            assert row.cancelled_at is None and row.cancellation_actor is None
            assert row.preparing_at is not None  # M14: stamped by the winning UPDATE itself

    assert sum(outcomes.values()) == ROUNDS


def test_same_slot_booking_exactly_one_wins(app, db):
    day = (db_today() + timedelta(days=1)).isoformat()

    for round_no in range(ROUNDS):
        table_id = create_table(f"RACE{round_no:02d}", 4).id
        # Fresh customers each round: the per-customer cap (M2) would
        # otherwise stop a repeat winner, and this test is about the slot.
        owners = [make_user(username=f"booker{round_no}_{i}", email=f"b{round_no}_{i}@example.com").id
                  for i in (1, 2)]

        def booker(owner_id):
            def act():
                try:
                    create_reservation(_db.session.get(User, owner_id), table_id, day, "19:00", 2)
                    return "ok"
                except ReservationError:
                    return "conflict"
            return act

        results = _race(app, booker(owners[0]), booker(owners[1]))
        assert sorted(map(str, results)) == ["conflict", "ok"], results
        _db.session.rollback()
        assert _db.session.query(Reservation).filter_by(table_id=table_id, holds_slot=True).count() == 1


def test_reservation_cap_holds_under_concurrent_bookings(app, db):
    """M2: one customer fires four bookings at once, each for a different free
    table. With the default cap of 2, exactly two may commit -- every round.
    The users-row lock in create_reservation is what decides it."""
    day = (db_today() + timedelta(days=2)).isoformat()
    limit = app.config["RESERVATION_ACTIVE_LIMIT"]
    attempts = limit + 2

    for round_no in range(ROUNDS):
        owner_id = make_user(username=f"flooder{round_no}", email=f"flood{round_no}@example.com").id
        table_ids = [create_table(f"CAP{round_no:02d}{i}", 4).id for i in range(attempts)]

        def booker(table_id):
            def act():
                try:
                    create_reservation(_db.session.get(User, owner_id), table_id, day, "19:00", 2)
                    return "ok"
                except ReservationError as exc:
                    return "capped" if "reservation" in exc.errors else f"other:{exc.errors}"
            return act

        results = _race(app, *(booker(t) for t in table_ids))
        assert sorted(map(str, results)) == ["capped"] * (attempts - limit) + ["ok"] * limit, results
        _db.session.rollback()  # end this session's snapshot before verifying
        assert _db.session.query(Reservation).filter_by(user_id=owner_id).count() == limit


def test_two_staff_seating_one_table_exactly_one_wins(app, db):
    """M3: two staff members submit a dine-in order for the same free table at
    the same instant. The table-row lock in checkout makes the second wait,
    re-read OCCUPIED and be refused -- never two open orders on one table."""
    from app.models.restaurant_table import RestaurantTable, TableStatus
    from app.services.orders import CheckoutError, place_staff_order
    from tests.conftest import make_menu_item

    staff_ids = [make_user(username=f"waiter{i}", email=f"waiter{i}@example.com", role=Role.STAFF).id
                 for i in (1, 2)]
    item_id = make_menu_item(name="Race Dish", price="100.00").id

    for round_no in range(ROUNDS):
        table_id = create_table(f"SEAT{round_no:02d}", 4).id

        def seat(staff_id):
            def act():
                try:
                    place_staff_order(_db.session.get(User, staff_id), {str(item_id): 1}, "dine_in", table_id)
                    return "ok"
                except CheckoutError as exc:
                    return "refused" if "table_id" in exc.errors else f"other:{exc.errors}"
            return act

        results = _race(app, seat(staff_ids[0]), seat(staff_ids[1]))
        assert sorted(map(str, results)) == ["ok", "refused"], results
        _db.session.rollback()  # end this session's snapshot before verifying
        assert _db.session.query(Order).filter_by(table_id=table_id).count() == 1
        assert _db.session.get(RestaurantTable, table_id).status == TableStatus.OCCUPIED


def test_one_time_code_redeemed_concurrently_exactly_once(app, db):
    """M4: two requests redeem the same reset code with different passwords at
    the same instant. The conditional UPDATE on used_at lets exactly one win;
    the stored password is the winner's."""
    from app.services.accounts import CodeRejected, issue_password_reset, redeem_code
    from app.utils.security import verify_password

    admin = make_user(username="codeadmin", email="codeadmin@example.com", role=Role.ADMIN)
    for round_no in range(ROUNDS):
        target = make_user(username=f"resetme{round_no}", email=f"resetme{round_no}@example.com")
        target_id = target.id
        code = issue_password_reset(admin, target)

        def redeemer(password):
            def act():
                try:
                    redeem_code(code, password, password)
                    return password
                except CodeRejected:
                    return "rejected"
            return act

        results = _race(app, redeemer("first-pass-111"), redeemer("second-pass-222"))
        assert sorted(results).count("rejected") == 1, results
        winner = next(r for r in results if r != "rejected")
        _db.session.rollback()
        assert verify_password(_db.session.get(User, target_id).password_hash, winner)


def test_two_admins_deactivating_each_other_leave_one_admin(app, db):
    """M4: the only two active admins deactivate each other at the same
    instant. The admin-row locks serialise them; the second sees the first's
    change and is refused, so an active admin always remains."""
    from app.services.accounts import AccountError, set_active

    for round_no in range(ROUNDS):
        a = make_user(username=f"adm_a{round_no}", email=f"adm_a{round_no}@example.com", role=Role.ADMIN).id
        b = make_user(username=f"adm_b{round_no}", email=f"adm_b{round_no}@example.com", role=Role.ADMIN).id
        # Earlier rounds' admins are deactivated directly, so each round starts with exactly two.
        _db.session.query(User).filter(User.role == Role.ADMIN, User.id.notin_([a, b])).update(
            {"is_active": False}, synchronize_session=False)
        _db.session.commit()

        def deactivate(actor_id, target_id):
            def act():
                try:
                    set_active(_db.session.get(User, actor_id), _db.session.get(User, target_id), False)
                    return "ok"
                except AccountError:
                    return "refused"
            return act

        results = _race(app, deactivate(a, b), deactivate(b, a))
        assert sorted(map(str, results)) == ["ok", "refused"], results
        _db.session.rollback()
        active = _db.session.query(User).filter(User.role == Role.ADMIN, User.is_active.is_(True)).count()
        assert active == 1


def test_two_staff_marking_one_dish_sold_out_change_it_once(app, db):
    """M5: two staff mark the same dish sold out at the same instant. The
    conditional UPDATE lets exactly one change it, so there is one audit row,
    and no other column of the item is touched."""
    from decimal import Decimal

    from app.models.audit_log import AuditEvent, AuditLog
    from app.models.menu_item import MenuItem
    from app.services.audit import record_event
    from app.services.menu import set_availability
    from tests.conftest import make_category, make_menu_item

    staff = [make_user(username=f"floor{i}", email=f"floor{i}@example.com", role=Role.STAFF).id for i in (1, 2)]
    category = make_category(name="Race")
    for round_no in range(ROUNDS):
        item_id = make_menu_item(category=category, name=f"Dish {round_no}", price="150.00").id

        def mark_sold_out(actor_id):
            def act():
                actor = _db.session.get(User, actor_id)
                return set_availability(actor, item_id, False, audit=lambda i, a: record_event(
                    AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED, success=True, user_id=actor_id,
                    metadata={"menu_item_id": i, "is_available": a}))
            return act

        results = _race(app, *(mark_sold_out(s) for s in staff))
        assert sorted(map(str, results)) == ["False", "True"], results
        _db.session.rollback()
        item = _db.session.get(MenuItem, item_id)
        _db.session.refresh(item)
        assert item.is_available is False and item.price == Decimal("150.00") and item.name == f"Dish {round_no}"
        rows = _db.session.query(AuditLog).filter_by(event_type=AuditEvent.MENU_ITEM_AVAILABILITY_CHANGED).all()
        assert sum(1 for r in rows if json.loads(r.metadata_json)["menu_item_id"] == item_id) == 1


# --- Milestone 11: payments ------------------------------------------------------------


def _paid_order_fixture():
    from tests.test_restaurant_ops import place

    customer = make_user(username="payer", email="payer@example.com")
    staff_ids = [make_user(username=f"till{i}", email=f"till{i}@example.com", role=Role.ADMIN).id for i in (1, 2)]
    return customer, staff_ids, place


def test_payment_vs_cancel_never_leaves_a_cancelled_order_holding_money(app, db):
    """M7.5.3: the cancellation guard (app/services/orders.py) now checks
    orders.amount_settled exclusively, so this race is against the
    ledger-backed record_ledger_payment -- the only payment-writing path
    left since app/services/payments.py was retired (M7.5.4.7)."""
    from app.services.payment_ledger import LedgerPaymentError, record_ledger_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order_id = place(customer).id

        def pay():
            try:
                order = _db.session.get(Order, order_id)
                record_ledger_payment(order, order.total, "card", _db.session.get(User, a))
                return "paid"
            except LedgerPaymentError:
                return "refused"

        def cancel():
            try:
                update_order_status(_db.session.get(Order, order_id), OrderStatus.CANCELLED,
                                    actor=_db.session.get(User, b))
                return "cancelled"
            except OrderStatusError:
                return "refused"

        results = _race(app, pay, cancel)
        assert sorted(map(str, results)) in (["paid", "refused"], ["cancelled", "refused"]), results
        _db.session.rollback()
        row = _db.session.get(Order, order_id)
        assert (row.status == OrderStatus.CANCELLED) != (row.amount_settled > 0), (row.status, row.amount_settled)


# --- Milestone 7.5.2: ledger-backed payments/refunds --------------------------------


def test_two_concurrent_partial_ledger_payments_cannot_overpay(app, db):
    """Unlike the legacy payments table (UNIQUE order_id), nothing stops two
    ledger 'payment' inserts for the same order at the database level --
    the order-row lock in record_ledger_payment is the only thing
    serialising them. Two partial payments that individually fit under the
    balance due but together would exceed it must not both succeed."""
    from decimal import Decimal

    from app.models.payment_ledger import PaymentLedgerEntry
    from app.services.payment_ledger import LedgerPaymentError, record_ledger_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order = place(customer, price="100.00", qty=1)
        order_id, total = order.id, order.total
        partial = (total * Decimal("0.6")).quantize(Decimal("0.01"))  # each < total, together > total

        def pay(staff_id):
            def act():
                try:
                    record_ledger_payment(_db.session.get(Order, order_id), partial, "cash",
                                          _db.session.get(User, staff_id))
                    return "ok"
                except LedgerPaymentError:
                    return "refused"
            return act

        results = _race(app, pay(a), pay(b))
        assert sorted(map(str, results)) == ["ok", "refused"], results
        _db.session.rollback()
        assert _db.session.get(Order, order_id).amount_settled == partial
        count = _db.session.query(PaymentLedgerEntry).filter_by(order_id=order_id).count()
        assert count == 1


def test_reconciliation_cannot_clobber_a_concurrent_genuine_ledger_payment(app, db):
    """M7.5.4: scripts/reconcile_payment_ledger.py races a genuine
    record_ledger_payment against the same order's legacy-payment backfill.
    Whichever wins the order-row lock first, the loser must detect the
    conflict *inside* that lock and back off cleanly -- never a duplicated
    backfill entry, never a clobbered amount_settled, never a CHECK
    violation. The legacy-shaped fixture row always charges the full total
    (app/services/payments.py's own record_payment used to guarantee this;
    M7.5.4.7 retired it, so the row is now built directly via
    make_legacy_payment, which mirrors the same default), so the two
    outcomes are deterministic: reconcile first leaves amount_settled at
    the full total and refuses the extra payment; the payment first leaves
    amount_settled at just that partial amount and reconcile backs off,
    leaving the legacy row for a later run."""
    from app.models.payment_ledger import PaymentLedgerEntry
    from app.services.payment_ledger import record_ledger_payment
    from scripts.reconcile_payment_ledger import _apply_one
    from tests.conftest import make_legacy_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order = place(customer, price="90.00", qty=1)
        legacy_payment = make_legacy_payment(order, _db.session.get(User, a))
        order_id, payment_id, total = order.id, legacy_payment.id, order.total

        def reconcile():
            return _apply_one(payment_id).action

        def pay_more():
            try:
                record_ledger_payment(_db.session.get(Order, order_id), "20.00", "card",
                                      _db.session.get(User, b))
                return "ok"
            except Exception:
                return "refused"

        results = _race(app, reconcile, pay_more)
        assert sorted(map(str, results)) in (["reconciled", "refused"], ["flagged", "ok"]), results
        _db.session.rollback()

        order_after = _db.session.get(Order, order_id)
        assert 0 <= order_after.amount_settled <= total
        entries = _db.session.query(PaymentLedgerEntry).filter_by(order_id=order_id).all()
        payment_entries = [e for e in entries if e.entry_type.value == "payment"]
        assert len(payment_entries) == 1, payment_entries


def test_reconciliation_payment_lock_is_real_against_a_concurrent_legacy_refund(app, db):
    """M7.5.4.4: regression for the M7.5.4.3 review finding that
    scripts/reconcile_payment_ledger.py's _apply_one read the legacy
    payment row's amount/refunded_amount from an object loaded before any
    lock was taken. The fix re-fetches and locks that row itself, after the
    order lock, with populate_existing=True -- SQLite accepts but does not
    enforce FOR UPDATE (see this module's own docstring), so only a real
    MySQL server proves this lock is an actual InnoDB row lock: a
    concurrent raw conditional UPDATE against this exact payments row
    (never locking the order, exactly the shape app/services/payments.py's
    now-retired refund_payment used -- M7.5.4.7 removed that module, so the
    race is reproduced here directly rather than through a service call)
    must either complete before reconcile's locked read -- which then
    correctly backfills the refund -- or block until reconcile's
    transaction ends.

    What this proves: whichever side's write reaches the payments row
    first, the ledger payment entry and amount_settled this run writes are
    always mutually consistent with each other (both read under the same
    lock, in the same transaction) -- never a torn mix of a stale amount
    and a fresh one, and never a duplicated or corrupted entry.

    What this does NOT, and structurally cannot, prove: a refund that
    commits strictly *after* reconciliation has already finished is a
    separate, pre-existing, documented gap -- a write with this shape never
    locks the order or touches the ledger, so nothing the reconcile script
    does can retroactively notice a write that had not happened yet when
    its own lock was released. This is precisely the risk M7.5.4.7 closed
    by retiring app/services/payments.py as a *live* writer; this test's
    raw UPDATE exists only to keep proving the row lock itself is real,
    not to suggest such a writer still exists in production."""
    from decimal import Decimal

    import sqlalchemy as sa

    from app.models.payment import Payment
    from app.models.payment_ledger import PaymentLedgerEntry
    from scripts.reconcile_payment_ledger import _apply_one
    from tests.conftest import make_legacy_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order = place(customer, price="60.00", qty=1)
        legacy_payment = make_legacy_payment(order, _db.session.get(User, a))
        order_id, payment_id = order.id, legacy_payment.id

        def reconcile():
            return _apply_one(payment_id).action

        def refund():
            try:
                stmt = (
                    sa.update(Payment)
                    .where(Payment.id == payment_id, Payment.refunded_amount + Decimal("10.00") <= Payment.amount)
                    .values(refunded_amount=Payment.refunded_amount + Decimal("10.00"), refunded_at=sa.func.now())
                    .execution_options(synchronize_session=False)
                )
                won = _db.session.execute(stmt).rowcount == 1
                _db.session.commit()
                return "refunded" if won else "refund-failed"
            except Exception:
                _db.session.rollback()
                return "refund-failed"

        results = _race(app, reconcile, refund)
        # Both always succeed regardless of which one reaches the payments
        # row first -- the loser waits on the row lock, it is never refused
        # or left to corrupt the other's work.
        assert sorted(map(str, results)) == ["reconciled", "refunded"], results
        _db.session.rollback()

        entries = _db.session.query(PaymentLedgerEntry).filter_by(order_id=order_id).all()
        payment_entries = [e for e in entries if e.entry_type.value == "payment"]
        refund_entries = [e for e in entries if e.entry_type.value == "refund"]
        assert len(payment_entries) == 1, payment_entries  # never duplicated, whichever order they ran in

        entry = payment_entries[0]
        order_after = _db.session.get(Order, order_id)
        # Internally consistent with itself, whatever this run saw under
        # its own lock -- the one guarantee the fix actually makes.
        assert order_after.amount_settled == entry.amount - (entry.refunded_amount or Decimal("0.00"))
        # Paired correctly either way: a refund entry exists if and only if
        # the payment entry's own refunded_amount reflects that refund.
        assert (len(refund_entries) == 1) == (entry.refunded_amount == Decimal("10.00"))
        if refund_entries:
            assert refund_entries[0].amount == Decimal("-10.00")


def test_two_full_ledger_refunds_cannot_overdraw(app, db):
    from decimal import Decimal

    from app.models.payment_ledger import PaymentLedgerEntry
    from app.services.payment_ledger import LedgerPaymentError, record_ledger_payment, refund_ledger_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order = place(customer, price="80.00", qty=1)
        payment = record_ledger_payment(order, order.total, "cash", _db.session.get(User, a))
        order_id, payment_id, full = order.id, payment.id, str(payment.amount)

        def refund():
            try:
                refund_ledger_payment(_db.session.get(PaymentLedgerEntry, payment_id), full, "race",
                                      _db.session.get(User, a))
                return "ok"
            except LedgerPaymentError:
                return "refused"

        results = _race(app, refund, refund)
        assert sorted(map(str, results)) == ["ok", "refused"], results
        _db.session.rollback()
        assert _db.session.get(PaymentLedgerEntry, payment_id).refunded_amount == Decimal(full)
        assert _db.session.get(Order, order_id).amount_settled == Decimal("0.00")


# --- Milestone 12: pricing ---------------------------------------------------------


def test_discount_vs_payment_payment_never_overcharges_a_stale_total(app, db):
    """Models the real staff workflow precisely: staff reads the order
    total (e.g. the balance due rendered on the order page), and only
    *afterwards* does a concurrent discount race against their submission
    of that already-read amount.

    M7.5.3 correction (M7.5.3.2 Part 1): a first version of this test read
    order.total *inside* pay(), immediately before calling
    record_ledger_payment -- which does not model a stale read at all,
    since nothing can have changed yet between that read and the lock
    attempt a few microseconds later (unlike the legacy record_payment,
    which always charged the freshly re-locked order.total itself and so
    truly could never be blocked by a discount). That version's
    `assert "paid" in results` was therefore still wrong in principle, even
    though it was unlikely to be caught by 15 rounds of a near-simultaneous
    race.

    Here, read_total is captured once, *before* the race starts (the
    "page load" moment), making a stale read structurally guaranteed, not
    probabilistic. Discounts only ever reduce total, so the two outcomes
    are deterministic by which operation wins the order-row lock:
      - pay() wins: amount_settled(0) + read_total <= total (unchanged,
        since discount() has not run yet) -> succeeds; discount() then
        sees amount_settled > 0 -> refused.
      - discount() wins: total drops below read_total -> pay()'s
        conditional UPDATE WHERE amount_settled + read_total <= total
        correctly fails -> refused, safely, with no ledger row written --
        never a payment recorded for more than the final total, and never
        a silently wrong one either.
    ("discounted", "paid") is not a reachable outcome under a true stale
    read, so it is deliberately not in the accepted outcome set below."""
    from app.models.payment_ledger import LedgerEntryType, PaymentLedgerEntry
    from app.services.payment_ledger import LedgerPaymentError, record_ledger_payment
    from app.services.pricing import PricingError, apply_discount

    customer, (a, b), place = _paid_order_fixture()
    outcomes = set()
    for _ in range(ROUNDS):
        order = place(customer)
        order_id = order.id
        read_total = order.total  # the stale amount "staff" read before either race leg starts

        def pay():
            try:
                record_ledger_payment(_db.session.get(Order, order_id), read_total, "cash",
                                      _db.session.get(User, a))
                return "paid"
            except LedgerPaymentError:
                return "pay-refused"

        def discount():
            try:
                apply_discount(_db.session.get(Order, order_id), "percent", "10", "race", _db.session.get(User, b))
                return "discounted"
            except PricingError:
                return "discount-refused"

        results = [str(r) for r in _race(app, pay, discount)]
        outcomes.add(tuple(sorted(results)))
        _db.session.rollback()
        order = _db.session.get(Order, order_id)
        entries = _db.session.query(PaymentLedgerEntry).filter_by(
            order_id=order_id, entry_type=LedgerEntryType.PAYMENT
        ).all()
        if "paid" in results:
            # Won the race before any discount applied: entry.amount is the
            # stale read_total, but nothing had changed yet, so it still
            # equals the (unchanged) final total -- never an overcharge.
            assert len(entries) == 1 and entries[0].amount == read_total == order.total, (results, order.total)
        else:
            # Lost the race to a discount: the stale amount now exceeds the
            # new total and must be safely refused, not silently accepted
            # or partially recorded.
            assert "pay-refused" in results, results
            assert entries == [], (results, entries)
            assert read_total > order.total, (read_total, order.total, results)
        assert order.amount_settled <= order.total  # never an overcharge, race or no race
        assert (order.discount_amount > 0) == ("discounted" in results)
    assert outcomes <= {("discount-refused", "paid"), ("discounted", "pay-refused")}


# --- Milestone 13: inventory -------------------------------------------------------


def test_concurrent_movements_and_sale_deductions_never_lose_an_update(app, db):
    """Atomic ``current_quantity = current_quantity + delta`` on a real server:
    a purchase and two order completions hitting the same ingredient at once
    must land exactly, and each change must have its ledger row."""
    from decimal import Decimal

    from app.models.ingredient import Ingredient
    from app.models.stock_movement import StockMovement
    from app.services.inventory import add_to_recipe, record_movement, save_ingredient
    from tests.conftest import make_category, make_menu_item
    from tests.test_restaurant_ops import set_status

    boss_id = make_user(username="stockboss", email="sb@example.com", role=Role.ADMIN).id
    buyer = make_user(username="stockbuyer", email="sbuy@example.com")
    boss = _db.session.get(User, boss_id)
    item = make_menu_item(category=make_category(name="Race Mains"), name="Race Burger", price="100.00")
    bun = save_ingredient(None, "race bun", "piece", "0", True)
    record_movement(bun, "purchase", "1000", "opening", boss)
    add_to_recipe(item, bun, "1")
    bun_id, item_id = bun.id, item.id

    expected = Decimal("1000")
    for _ in range(ROUNDS):
        from app.services.orders import checkout
        orders = [checkout(buyer.id, {str(item_id): 2}).id for _ in range(2)]
        for oid in orders:
            set_status(_db.session.get(Order, oid), OrderStatus.READY)

        def complete(oid):
            def act():
                update_order_status(_db.session.get(Order, oid), OrderStatus.COMPLETED,
                                    actor=_db.session.get(User, boss_id))
                return "done"
            return act

        def purchase():
            record_movement(_db.session.get(Ingredient, bun_id), "purchase", "5", "race", _db.session.get(User, boss_id))
            return "bought"

        results = _race(app, complete(orders[0]), complete(orders[1]), purchase)
        assert sorted(map(str, results)) == ["bought", "done", "done"], results
        expected += Decimal("5") - Decimal("4")
        _db.session.rollback()
        assert _db.session.get(Ingredient, bun_id).current_quantity == expected
        ledger = sum(m.quantity_change for m in _db.session.query(StockMovement).filter_by(ingredient_id=bun_id))
        assert ledger == expected  # every change is on the ledger


def test_two_orders_racing_to_complete_cannot_oversell_a_scarce_ingredient(app, db):
    """M7.4: completion (deduct_for_order) is the authoritative stock gate,
    not checkout. Two orders that together need more of an ingredient than
    exists must never both complete -- the row lock it takes on the
    ingredient serialises the two completions, and exactly one must be
    refused outright (OrderStatusError), stock never going negative and
    getting exactly one SALE row, not two."""
    from decimal import Decimal

    from app.models.ingredient import Ingredient
    from app.models.stock_movement import MovementType, StockMovement
    from app.services.inventory import add_to_recipe, record_movement, save_ingredient
    from app.services.orders import checkout
    from tests.conftest import make_category, make_menu_item
    from tests.test_restaurant_ops import set_status

    boss_id = make_user(username="scarceboss", email="scarceboss@example.com", role=Role.ADMIN).id
    buyer = make_user(username="scarcebuyer", email="scarcebuyer@example.com")
    boss = _db.session.get(User, boss_id)
    item = make_menu_item(category=make_category(name="Race Scarce"), name="Race Special", price="50.00")
    bean = save_ingredient(None, "race bean", "piece", "0", True)
    add_to_recipe(item, bean, "1")
    item_id, bean_id = item.id, bean.id

    for round_no in range(ROUNDS):
        # Exactly enough stock for one of the two orders about to race.
        record_movement(_db.session.get(Ingredient, bean_id), "purchase", "1", f"race {round_no}", boss)
        orders = [checkout(buyer.id, {str(item_id): 1}).id for _ in range(2)]
        for oid in orders:
            set_status(_db.session.get(Order, oid), OrderStatus.READY)

        def complete(oid):
            def act():
                try:
                    update_order_status(_db.session.get(Order, oid), OrderStatus.COMPLETED,
                                        actor=_db.session.get(User, boss_id))
                    return "done"
                except OrderStatusError:
                    return "refused"
            return act

        results = _race(app, complete(orders[0]), complete(orders[1]))
        assert sorted(map(str, results)) == ["done", "refused"], results
        _db.session.rollback()  # end this session's snapshot before verifying

        assert _db.session.get(Ingredient, bean_id).current_quantity == Decimal("0"), (
            "stock must never go negative -- the loser must be refused, not deducted"
        )
        # _race() appends to its results list in whichever order the two
        # threads actually finish, not in the order their actions were
        # passed in -- so results[0] cannot be assumed to be orders[0]'s
        # outcome. The persisted status of each order is what actually
        # happened and is read fresh here instead.
        statuses = {oid: _db.session.get(Order, oid).status for oid in orders}
        completed = [oid for oid, status in statuses.items() if status == OrderStatus.COMPLETED]
        ready = [oid for oid, status in statuses.items() if status == OrderStatus.READY]
        assert len(completed) == 1 and len(ready) == 1, statuses  # exactly one winner, one refused loser
        winner_id, loser_id = completed[0], ready[0]
        sales_this_round = _db.session.query(StockMovement).filter_by(
            ingredient_id=bean_id, movement_type=MovementType.SALE, order_id=winner_id).count()
        assert sales_this_round == 1
        assert _db.session.query(StockMovement).filter_by(
            ingredient_id=bean_id, movement_type=MovementType.SALE, order_id=loser_id).count() == 0
