"""M1: ``flask create-admin`` -- production admin bootstrap.

Driven through Flask's CLI runner against the ordinary test database (SQLite
by default, MySQL when TEST_DATABASE_URL is set), so the command, the service
and the transaction are exercised exactly as an operator would run them.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import OperationalError

from app import cli as cli_module
from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.user import Role, User
from app.services import auth as auth_service
from app.utils.security import verify_password
from tests.conftest import make_user

PASSWORD = "Bootstrap-pass-42"


@pytest.fixture
def run(app, monkeypatch, installation_identity):
    """Run ``flask create-admin`` with prompt answers, as if from a terminal.
    Since O2 the command also needs the installation identity a migrated
    database always has (``installation_identity``)."""
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    runner = app.test_cli_runner()

    def _run(username="owner", email="owner@example.com", password=PASSWORD, confirm=PASSWORD):
        return runner.invoke(args=["create-admin"], input=f"{username}\n{email}\n{password}\n{confirm}\n")

    return _run


def _admin_audit_rows():
    return _db.session.query(AuditLog).filter_by(event_type=AuditEvent.ADMIN_CREATED).all()


def _assert_nothing_created(user_count_before: int) -> None:
    _db.session.rollback()
    assert _db.session.query(User).count() == user_count_before
    assert _admin_audit_rows() == []


def test_creates_admin_with_hashed_password_and_audit(run):
    result = run(username="Owner_1", email="Owner@Example.com")

    assert result.exit_code == 0, result.output
    assert "Administrator created successfully." in result.output
    assert "Username: owner_1" in result.output
    assert PASSWORD not in result.output  # hidden prompts are not echoed, nothing printed

    user = _db.session.query(User).filter_by(username="owner_1").one()
    assert user.role == Role.ADMIN
    assert user.is_active
    assert user.email == "owner@example.com"
    assert user.password_hash != PASSWORD and PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(user.password_hash, PASSWORD)

    [row] = _admin_audit_rows()
    assert row.user_id == user.id and row.success
    assert json.loads(row.metadata_json) == {"source": "cli"}
    stored = f"{row.metadata_json} {row.user_agent} {row.ip_address}"
    assert PASSWORD not in stored and user.password_hash not in stored


def test_new_admin_can_sign_in_to_admin_portal(run, client):
    assert run().exit_code == 0
    response = client.post("/login/admin", data={"username": "owner", "password": PASSWORD})
    assert response.status_code == 302 and response.headers["Location"].endswith("/account/")


@pytest.mark.parametrize("field", ["username", "email"])
def test_duplicate_username_or_email_is_rejected(run, field):
    make_user(username="taken", email="taken@example.com", role=Role.CUSTOMER)
    before = _db.session.query(User).count()

    kwargs = {"username": "TAKEN"} if field == "username" else {"email": "Taken@Example.com"}
    result = run(**kwargs)

    assert result.exit_code == 1
    assert "Administrator NOT created" in result.output
    assert "already" in result.output
    _assert_nothing_created(before)
    # The existing account was not promoted.
    assert _db.session.query(User).filter_by(username="taken").one().role == Role.CUSTOMER


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"password": "short1", "confirm": "short1"}, "at least 10 characters"),
        ({"password": "password123", "confirm": "password123"}, "too common"),
        ({"confirm": PASSWORD + "x"}, "Passwords do not match"),
        ({"username": "  "}, "Username must be"),
        ({"username": "bad name!"}, "Username must be"),
        ({"email": "  "}, "valid email"),
        ({"email": "not-an-email"}, "valid email"),
    ],
)
def test_invalid_input_is_rejected_without_side_effects(run, kwargs, message):
    result = run(**kwargs)

    assert result.exit_code == 1
    assert message in result.output
    assert PASSWORD not in result.output
    _assert_nothing_created(0)


def test_refuses_without_an_interactive_terminal(app, monkeypatch):
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)
    result = app.test_cli_runner().invoke(args=["create-admin"], input=f"owner\no@example.com\n{PASSWORD}\n{PASSWORD}\n")

    assert result.exit_code != 0
    assert "interactive terminal" in result.output
    _assert_nothing_created(0)


def test_database_failure_rolls_back_and_prints_no_statement_parameters(run, monkeypatch):
    """The account and its audit row share one transaction, and a database
    error is reported without the statement parameters SQLAlchemy would
    otherwise print (which can include the password hash)."""
    def broken_record_event(*args, **kwargs):
        raise OperationalError("INSERT INTO audit_logs ...", {"password_hash": "HASH-MARKER"}, Exception("lost"))

    monkeypatch.setattr("app.services.audit.record_event", broken_record_event)
    result = run()

    assert result.exit_code == 1
    assert "database error (OperationalError)" in result.output
    assert "HASH-MARKER" not in result.output and PASSWORD not in result.output
    _assert_nothing_created(0)


def test_lost_duplicate_race_creates_nothing(run, monkeypatch):
    """If another writer takes the username between the pre-check and the
    insert, the unique constraint wins and neither the account nor the audit
    row is left behind."""
    make_user(username="owner", email="first@example.com")
    monkeypatch.setattr(auth_service, "_duplicate_errors", lambda *args: {})

    result = run(username="owner", email="second@example.com")

    assert result.exit_code == 1
    assert "already registered" in result.output
    _assert_nothing_created(1)
