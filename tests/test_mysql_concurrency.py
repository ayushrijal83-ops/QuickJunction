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


# --- Milestone 12: pricing ---------------------------------------------------------


def test_discount_vs_payment_payment_always_matches_final_total(app, db):
    """Both may succeed (discount first, then payment at the new total), or the
    payment wins and the discount is refused -- but a payment must never be
    taken at a total that then changes underneath it."""
    from app.models.payment import Payment
    from app.services.payments import PaymentError, record_payment
    from app.services.pricing import PricingError, apply_discount

    customer, (a, b), place = _paid_order_fixture()
    outcomes = set()
    for _ in range(ROUNDS):
        order_id = place(customer).id

        def pay():
            try:
                record_payment(_db.session.get(Order, order_id), "cash", _db.session.get(User, a))
                return "paid"
            except PaymentError:
                return "pay-refused"

        def discount():
            try:
                apply_discount(_db.session.get(Order, order_id), "percent", "10", "race", _db.session.get(User, b))
                return "discounted"
            except PricingError:
                return "discount-refused"

        results = [str(r) for r in _race(app, pay, discount)]
        assert "paid" in results, results  # the payment itself is never blocked by a discount
        outcomes.add(tuple(sorted(results)))
        _db.session.rollback()
        order = _db.session.get(Order, order_id)
        payment = _db.session.query(Payment).filter_by(order_id=order_id).one()
        assert payment.amount == order.total, (payment.amount, order.total, results)
        assert (order.discount_amount > 0) == ("discounted" in results)
    assert outcomes <= {("discount-refused", "paid"), ("discounted", "paid")}


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
