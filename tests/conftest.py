"""Shared pytest fixtures.

The testing configuration points at an in-memory SQLite database, so the
suite runs with no MySQL server and touches no real data.
"""

from __future__ import annotations

import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from flask import Flask
from flask.testing import FlaskClient

from decimal import Decimal

from app import create_app
from app.extensions import db as _db
from app.models.category import Category
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.models.menu_item import MenuItem
from app.models.user import Role, User
from app.utils.security import hash_password

_CSRF_TAG_RE = re.compile(r'<input[^>]*name="csrf_token"[^>]*>')
_VALUE_RE = re.compile(r'value="([^"]+)"')


def assert_disposable_test_database(uri: str) -> None:
    """Every test drop_all()s its database. An opt-in MySQL run
    (TEST_DATABASE_URL) must therefore target a dedicated ``*_test`` database
    and never the application's own DATABASE_URL."""
    url = make_url(uri)
    if url.get_backend_name() == "sqlite":
        return
    app_db = make_url(os.environ["DATABASE_URL"]).database if os.environ.get("DATABASE_URL") else None
    assert url.database and url.database.endswith("_test") and url.database != app_db, (
        f"refusing to run tests against database {url.database!r}")


def rejected_by_check_constraint():
    """A CHECK violation surfaces as IntegrityError on SQLite but as
    OperationalError (errno 3819) through PyMySQL. The message match keeps a
    genuine operational failure (e.g. a lost connection) from passing."""
    return pytest.raises((IntegrityError, OperationalError), match=r"(?i)check constraint")


@pytest.fixture(autouse=True)
def _fresh_schema_for_mysql_migration_tests(request):
    """Migration tests assume an empty database. On SQLite each gets a fresh
    in-memory one for free; an opt-in MySQL run shares one ``*_test``
    database, so wipe it first or one test's leftovers break the next."""
    uri = os.environ.get("TEST_DATABASE_URL", "")
    if "migration" in request.module.__name__ and uri.startswith("mysql"):
        import sqlalchemy as sa

        assert_disposable_test_database(uri)
        engine = sa.create_engine(uri)
        with engine.begin() as conn:
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))
            for table in sa.inspect(conn).get_table_names():
                conn.execute(sa.text(f"DROP TABLE `{table}`"))  # names come from the server, not input
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 1"))
        engine.dispose()
    yield


def _register_test_probes(application: Flask) -> None:
    """Identity/role probes for the auth-boundary tests. They used to be real
    routes (``/account/me|staff|admin``); M2 removed those from the product,
    so they exist only on the test app and are never registered by
    ``create_app``."""
    from flask import Blueprint, jsonify

    from app.utils.authorization import get_current_user, login_required, require_role

    probes = Blueprint("test_probes", __name__, url_prefix="/_test")

    @probes.get("/me")
    @login_required
    def me():
        user = get_current_user()
        return jsonify(username=user.username, role=user.role.value)

    @probes.get("/staff")
    @require_role(Role.STAFF, Role.ADMIN)
    def staff_only():
        return jsonify(message="staff access confirmed")

    @probes.get("/admin")
    @require_role(Role.ADMIN)
    def admin_only():
        return jsonify(message="admin access confirmed")

    application.register_blueprint(probes)


@pytest.fixture
def app() -> Flask:
    application = create_app("testing")
    _register_test_probes(application)
    assert_disposable_test_database(application.config["SQLALCHEMY_DATABASE_URI"])
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(autouse=True)
def _clear_ledger_self_references(request):
    """SQLite enforces ON DELETE RESTRICT even while dropping the whole
    self-referencing payment_ledger_entries table (a real sqlite3
    limitation -- 'FOREIGN KEY constraint failed' straight out of DROP
    TABLE; InnoDB has no such issue dropping a table regardless of its own
    FK content). Break the self-reference (a refund's reversed_entry_id)
    before the ``app`` fixture's drop_all() runs; this changes nothing
    about the RESTRICT the schema itself enforces at runtime. Global
    (M7.5.3): any test that records a ledger refund needs this, not only
    the payment-ledger model/service test files.

    M7.5.3.7 correction: this used to depend on ``app`` directly, which
    forced pytest to activate the shared app/db fixture -- and so the
    shared TEST_DATABASE_URL database under CI -- for *every* test in the
    suite, including ones that build their own, independent application
    (tests/test_payment_ledger_migration.py's ``_app()`` helper, now
    correctly isolated to its own SQLite engine). Those tests never
    request ``app``/``db`` as pytest fixtures at all, so under MySQL CI
    this surfaced as a teardown error ('payment_ledger_entries doesn't
    exist') against the shared database they were never touching on
    purpose. ``request.fixturenames`` lists ``app`` whenever a test
    requests it directly or via ``db`` (which depends on it); checking
    that first and fetching ``app`` lazily only when it is already needed
    preserves the exact same cleanup, in the exact same order relative to
    ``app``'s own ``drop_all()`` (``getfixturevalue()`` registers that
    dependency too), without ever activating ``app`` for a test that
    never asked for it.

    The fetch happens *before* ``yield``, not after: ``getfixturevalue()``
    only registers a correct teardown-ordering dependency when called
    during setup. Called after ``yield`` instead, ``app`` -- having no
    declared relationship to this fixture -- can and did finalize first,
    making this fixture's own teardown run against an already-torn-down
    fixture ('fixture value for "app" is not available')."""
    needs_cleanup = "app" in request.fixturenames
    if needs_cleanup:
        request.getfixturevalue("app")
    yield
    if not needs_cleanup:
        return
    _db.session.execute(sa.text("DELETE FROM payment_ledger_entries WHERE reversed_entry_id IS NOT NULL"))
    _db.session.execute(sa.text("DELETE FROM payment_ledger_entries"))
    _db.session.commit()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def db(app: Flask):
    return _db


def make_user(username: str = "alice", email: str = "alice@example.com", password: str = "correct-horse-1",
              role: Role = Role.CUSTOMER, is_active: bool = True,
              staff_approved: bool | None = None) -> User:
    """Insert a user directly, bypassing HTTP -- for tests that need an
    account to already exist (login, authorization checks).

    A directly inserted STAFF account stands for one an operator provisioned,
    so it is approved unless the test asks for a pending one."""
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role=role,
        is_active=is_active,
        staff_approved=(role == Role.STAFF) if staff_approved is None else staff_approved,
    )
    _db.session.add(user)
    _db.session.commit()
    return user


def make_category(name: str = "Starters", is_active: bool = True) -> Category:
    category = Category(name=name, description=None, is_active=is_active)
    _db.session.add(category)
    _db.session.commit()
    return category


def make_menu_item(
    category: Category | None = None,
    name: str = "Paneer Tikka",
    price: str = "199.00",
    cuisine: Cuisine = Cuisine.INDIAN,
    spice_level: SpiceLevel = SpiceLevel.MEDIUM,
    dietary_type: DietaryType = DietaryType.VEGETARIAN,
    is_available: bool = True,
) -> MenuItem:
    """Insert a menu item directly, bypassing HTTP/the service layer -- for
    tests that need one to already exist."""
    if category is None:
        category = make_category()
    item = MenuItem(
        category_id=category.id,
        name=name,
        description=None,
        price=Decimal(price),
        cuisine=cuisine,
        spice_level=spice_level,
        dietary_type=dietary_type,
        is_available=is_available,
    )
    _db.session.add(item)
    _db.session.commit()
    return item


def make_legacy_payment(order, actor, *, method: str = "cash", amount: str | None = None):
    """Insert a legacy ``payments`` row directly, bypassing
    app/services/payments.py (retired M7.5.4.7) -- for tests that need the
    pre-cutover 'payments row, no ledger entry' shape
    scripts/reconcile_payment_ledger.py exists to repair. Mirrors the
    retired ``record_payment``'s own defaults: the full order total, method
    ``cash`` unless given otherwise."""
    from app.models.payment import Payment, PaymentMethod

    payment = Payment(
        order_id=order.id,
        method=PaymentMethod(method),
        amount=Decimal(amount) if amount is not None else order.total,
        recorded_by_id=actor.id,
    )
    _db.session.add(payment)
    _db.session.commit()
    return payment


def refund_legacy_payment(payment, amount, reason: str | None = None):
    """Apply a refund directly to a legacy ``payments`` row, bypassing the
    retired ``refund_payment``. Not a re-implementation of its validation --
    callers needing to exercise refund-bound checking use
    ``refund_ledger_payment`` instead; this is a fixture-construction
    convenience only."""
    from datetime import datetime

    payment.refunded_amount = Decimal(str(amount))
    payment.refunded_at = datetime.utcnow()
    payment.refund_reason = reason
    _db.session.commit()
    return payment


def csrf_token(client: FlaskClient, get_url: str) -> str:
    """Fetch a form page and pull its CSRF token, the way a browser would
    submit it back."""
    response = client.get(get_url)
    tag_match = _CSRF_TAG_RE.search(response.get_data(as_text=True))
    assert tag_match, f"no CSRF field found on {get_url}"
    value_match = _VALUE_RE.search(tag_match.group(0))
    assert value_match, f"CSRF field on {get_url} has no value"
    return value_match.group(1)
