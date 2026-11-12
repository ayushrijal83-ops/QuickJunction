"""S1-B pre-CI fix B1: login refuses usernames no account can have.

Every account's username matches USERNAME_PATTERN, so authenticate_user
refuses any other name without a database lookup (after the usual dummy
hash comparison). On MySQL that lookup would otherwise match accented
spellings to an ASCII account through the accent-insensitive collation --
and each spelling has its own login rate-limit key. The MySQL proof is in
tests/test_mysql_username_collation.py; these tests run on every backend.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import app.services.auth as auth_service
from app.extensions import db as _db
from app.services.auth import authenticate_user
from app.utils.ratelimit import login_limiter
from tests.conftest import make_user

PASSWORD = "correct-horse-1"
INVALID_NAMES = ["alicé", "àlice", "ALİCE", "ａｌｉｃｅ", "al", "a" * 33, "ali ce", "alice!", ""]


@pytest.fixture
def statements(db):
    """Every SQL statement the engine executes while the test runs."""
    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)

    sa.event.listen(_db.engine, "before_cursor_execute", record)
    yield seen
    sa.event.remove(_db.engine, "before_cursor_execute", record)


def test_valid_usernames_authenticate_exactly_as_before(db):
    alice = make_user(username="alice", password=PASSWORD)
    make_user(username="bob_99", email="bob@example.com", password=PASSWORD)
    assert authenticate_user("alice", PASSWORD).id == alice.id
    assert authenticate_user("  ALICE ", PASSWORD).id == alice.id  # trimmed and lowercased, as before
    assert authenticate_user("Bob_99", PASSWORD).username == "bob_99"
    assert authenticate_user("alice", "wrong-password-1") is None


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_names_outside_the_username_pattern_never_authenticate(db, name):
    make_user(username="alice", password=PASSWORD)
    assert authenticate_user(name, PASSWORD) is None


def test_an_invalid_name_gets_the_dummy_comparison_and_no_lookup(db, statements, monkeypatch):
    make_user(username="alice", password=PASSWORD)
    compared: list[str] = []
    real_verify = auth_service.verify_password

    def spy(stored_hash, password):
        compared.append(stored_hash)
        return real_verify(stored_hash, password)

    monkeypatch.setattr(auth_service, "verify_password", spy)
    statements.clear()
    assert authenticate_user("alicé", PASSWORD) is None
    assert compared == [auth_service._DUMMY_HASH]  # same work as an unknown username
    assert statements == []  # and the database was never asked

    compared.clear()
    assert authenticate_user("nobody", PASSWORD) is None  # a valid but unknown name: unchanged path
    assert compared == [auth_service._DUMMY_HASH]


def test_login_route_refuses_an_accented_variant_and_counts_it(client, db):
    make_user(username="alice", password=PASSWORD)
    response = client.post("/login", data={"username": "alicé", "password": PASSWORD})
    assert response.status_code == 200
    assert "Invalid username or password." in response.get_data(as_text=True)  # the usual generic failure
    assert len(login_limiter._attempts["127.0.0.1:alicé"]) == 1  # a failed attempt like any other
    assert client.post("/login", data={"username": "alice", "password": PASSWORD}).status_code == 302
