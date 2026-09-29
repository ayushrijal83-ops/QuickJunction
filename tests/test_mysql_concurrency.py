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
        else:
            assert row.status == OrderStatus.PREPARING
            assert row.cancelled_at is None and row.cancellation_actor is None

    assert sum(outcomes.values()) == ROUNDS


def test_same_slot_booking_exactly_one_wins(app, db):
    owners = [make_user(username=f"booker{i}", email=f"b{i}@example.com").id for i in (1, 2)]
    day = (db_today() + timedelta(days=1)).isoformat()

    for round_no in range(ROUNDS):
        table_id = create_table(f"RACE{round_no:02d}", 4).id

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


# --- Milestone 11: payments ------------------------------------------------------------


def _paid_order_fixture():
    from tests.test_restaurant_ops import place

    customer = make_user(username="payer", email="payer@example.com")
    staff_ids = [make_user(username=f"till{i}", email=f"till{i}@example.com", role=Role.ADMIN).id for i in (1, 2)]
    return customer, staff_ids, place


def test_two_cashiers_cannot_both_take_payment(app, db):
    from app.models.payment import Payment
    from app.services.payments import PaymentError, record_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order_id = place(customer).id

        def pay(staff_id):
            def act():
                try:
                    record_payment(_db.session.get(Order, order_id), "cash", _db.session.get(User, staff_id))
                    return "ok"
                except PaymentError:
                    return "refused"
            return act

        results = _race(app, pay(a), pay(b))
        assert sorted(map(str, results)) == ["ok", "refused"], results
        _db.session.rollback()
        assert _db.session.query(Payment).filter_by(order_id=order_id).count() == 1


def test_payment_vs_cancel_never_leaves_a_cancelled_order_holding_money(app, db):
    from app.models.payment import Payment
    from app.services.payments import PaymentError, record_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order_id = place(customer).id

        def pay():
            try:
                record_payment(_db.session.get(Order, order_id), "card", _db.session.get(User, a))
                return "paid"
            except PaymentError:
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
        paid = _db.session.query(Payment).filter_by(order_id=order_id).count()
        assert (row.status == OrderStatus.CANCELLED) != (paid == 1), (row.status, paid)


def test_two_full_refunds_cannot_overdraw(app, db):
    from decimal import Decimal

    from app.models.payment import Payment
    from app.services.payments import PaymentError, record_payment, refund_payment

    customer, (a, b), place = _paid_order_fixture()
    for _ in range(ROUNDS):
        order = place(customer)
        payment_id = record_payment(order, "cash", _db.session.get(User, a)).id
        full = str(order.total)

        def refund():
            try:
                refund_payment(_db.session.get(Payment, payment_id), full, None)
                return "ok"
            except PaymentError:
                return "refused"

        results = _race(app, refund, refund)
        assert sorted(map(str, results)) == ["ok", "refused"], results
        _db.session.rollback()
        assert _db.session.get(Payment, payment_id).refunded_amount == Decimal(full)
