"""O3.1 test-only helper: one realistic installation for the authorization
baseline (tests/test_authz_baseline_*.py). Not a test module.

Builds, in the ordinary test database (``create_all()`` plus the O1/O2
identity rows a migrated database always has):

- one account per access class -- customer, approved staff, admin, inactive
  staff/admin, pending staff, and an admin whose ``staff_approved`` is false
  (as every ``flask create-admin`` admin is) -- plus a second customer and
  separate *target* accounts for the admin account-management routes;
- a category, a menu item, an ingredient, two tables;
- orders placed through the real checkout (customer A's open order, customer
  B's order, and a paid order with a ledger payment to refund);
- reservations for customer A and customer B.

Passwords are hashed once per process (Argon2 is deliberately slow) and the
accounts are inserted directly, so building a world costs milliseconds and
every test case can afford its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal

from app.extensions import db
from app.models.category import Category
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.models.ingredient import Ingredient, StockUnit
from app.models.installation import Branch, Installation, Organization
from app.models.menu_item import MenuItem
from app.models.order import Order, OrderSource
from app.models.payment_ledger import PaymentLedgerEntry
from app.models.reservation import Reservation, ReservationStatus
from app.models.restaurant_table import RestaurantTable
from app.models.user import Role, User
from app.services.orders import checkout
from app.services.organization import enroll_new_account
from app.services.payment_ledger import record_ledger_payment
from app.utils.clock import db_today
from app.utils.security import hash_password

PASSWORD = "Baseline-pass-42"
_hash_cache: list[str] = []


def password_hash() -> str:
    if not _hash_cache:
        _hash_cache.append(hash_password(PASSWORD))
    return _hash_cache[0]


# Access classes. "effective" is what get_current_user() makes of a session
# for that account today: inactive accounts and pending STAFF are signed
# out (treated as anonymous); staff_approved means nothing for ADMIN.
CLASSES = {
    # name:            (role,          is_active, staff_approved, effective)
    "anonymous":       (None,          None,      None,           "anonymous"),
    "customer":        (Role.CUSTOMER, True,      False,          "customer"),
    "staff":           (Role.STAFF,    True,      True,           "staff"),
    "admin":           (Role.ADMIN,    True,      True,           "admin"),
    "inactive_staff":  (Role.STAFF,    False,     True,           "anonymous"),
    "inactive_admin":  (Role.ADMIN,    False,     True,           "anonymous"),
    "pending_staff":   (Role.STAFF,    True,      False,          "anonymous"),
    "pending_admin":   (Role.ADMIN,    True,      False,          "admin"),
}


@dataclass
class World:
    accounts: dict[str, User]
    customer_b: User
    target_staff: User          # active, approved staff: reset / deactivate / revoke-approval target
    target_inactive: User       # inactive staff: activate target
    target_pending: User        # pending staff: approve target
    organization: Organization
    branch: Branch
    category: Category
    item: MenuItem
    ingredient: Ingredient
    table: RestaurantTable
    table_b: RestaurantTable
    order: Order                # customer A, open (pending), unpaid
    order_b: Order              # customer B
    paid_order: Order           # customer A, fully paid by staff
    payment_entry: PaymentLedgerEntry
    reservation: Reservation    # customer A, confirmed, tomorrow 19:00 at `table`
    reservation_b: Reservation  # customer B, confirmed, tomorrow 19:00 at `table_b`


def _user(username: str, role: Role, *, active: bool = True, approved: bool | None = None) -> User:
    user = User(username=username, email=f"{username}@example.com", password_hash=password_hash(), role=role,
                is_active=active, staff_approved=(role == Role.STAFF) if approved is None else approved)
    db.session.add(user)
    db.session.flush()
    return user


def build_world() -> World:
    organization = Organization(name="Quick Junction")
    branch = Branch(organization=organization, name="Quick Junction")
    db.session.add_all([organization, branch, Installation(id=1, branch=branch)])
    db.session.flush()

    accounts = {}
    for name, (role, active, approved, _) in CLASSES.items():
        if role is not None:
            accounts[name] = _user(name, role, active=active, approved=approved)
    customer_b = _user("customer_b", Role.CUSTOMER)
    target_staff = _user("target_staff", Role.STAFF)
    target_inactive = _user("target_inactive", Role.STAFF, active=False)
    target_pending = _user("target_pending", Role.STAFF, approved=False)
    # Workforce accounts are organization members, as on a migrated database.
    for user in [*(u for u in accounts.values() if u.role != Role.CUSTOMER),
                 target_staff, target_inactive, target_pending]:
        enroll_new_account(user, organization, branch)

    category = Category(name="Mains", description=None, is_active=True)
    db.session.add(category)
    db.session.flush()
    item = MenuItem(category_id=category.id, name="Dal Bhat", description=None, price=Decimal("200.00"),
                    cuisine=Cuisine.INDIAN, spice_level=SpiceLevel.MEDIUM, dietary_type=DietaryType.VEGETARIAN,
                    is_available=True)
    ingredient = Ingredient(name="rice", unit=StockUnit.KILOGRAM, current_quantity=Decimal("50"))
    table = RestaurantTable(name="T1", capacity=4)
    table_b = RestaurantTable(name="T2", capacity=4)
    db.session.add_all([item, ingredient, table, table_b])
    db.session.commit()

    customer = accounts["customer"]
    order = checkout(customer.id, {str(item.id): 1}, OrderSource.ONLINE, None)
    order_b = checkout(customer_b.id, {str(item.id): 1}, OrderSource.ONLINE, None)
    paid_order = checkout(customer.id, {str(item.id): 2}, OrderSource.ONLINE, None)
    payment_entry = record_ledger_payment(paid_order, str(paid_order.total), "cash", accounts["staff"])

    tomorrow = db_today() + timedelta(days=1)
    reservation = Reservation(user_id=customer.id, table_id=table.id, reservation_date=tomorrow,
                              reservation_time=time(19, 0), guest_count=2, status=ReservationStatus.CONFIRMED,
                              holds_slot=True)
    reservation_b = Reservation(user_id=customer_b.id, table_id=table_b.id, reservation_date=tomorrow,
                                reservation_time=time(19, 0), guest_count=2, status=ReservationStatus.CONFIRMED,
                                holds_slot=True)
    db.session.add_all([reservation, reservation_b])
    db.session.commit()

    return World(accounts=accounts, customer_b=customer_b, target_staff=target_staff,
                 target_inactive=target_inactive, target_pending=target_pending, organization=organization,
                 branch=branch, category=category, item=item, ingredient=ingredient, table=table, table_b=table_b,
                 order=order, order_b=order_b, paid_order=paid_order, payment_entry=payment_entry,
                 reservation=reservation, reservation_b=reservation_b)


def sign_in(client, user: User | None) -> None:
    """Give ``client`` exactly the session ``login_user`` creates for
    ``user`` (user id + session version), or none for anonymous. Used so the
    route matrix exercises the per-request gate (get_current_user + the
    decorators) and not the login form, rate limiter and portal rules,
    which tests/test_authz_baseline_services.py pins separately."""
    if user is None:
        return
    with client.session_transaction() as session:
        session["user_id"] = user.id
        session["sv"] = user.session_version
