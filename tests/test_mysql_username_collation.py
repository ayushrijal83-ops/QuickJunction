"""S1-B pre-CI fix B1, proven on MySQL: an accented spelling cannot sign in
as an existing ASCII account under the database's own collation.

Runs only when ``TEST_DATABASE_URL`` points at MySQL (skipped on the default
SQLite, which compares usernames byte for byte). CI's MySQL job runs this
file on its own and fails unless all 3 tests pass -- none may be skipped.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.extensions import db as _db
from app.models.user import User
from app.services.auth import authenticate_user
from tests.conftest import make_user

PASSWORD = "correct-horse-1"
VARIANTS = ["alicé", "àlice", "álice", "alicè", "ÁLICÉ", "alïce"]


@pytest.fixture(autouse=True)
def _mysql_only(app):
    if _db.engine.dialect.name != "mysql":
        pytest.skip("collation verification needs TEST_DATABASE_URL=mysql+pymysql://...")


def test_the_configured_collation_matches_accented_variants(db):
    """The hazard is real here: the column's collation treats the variants
    as the same username, so a raw lookup by any of them finds the account.
    If this ever fails, the collation changed and B1 needs re-evaluating."""
    alice = make_user(username="alice", password=PASSWORD)
    collation = _db.session.execute(sa.text(
        "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'username'")).scalar_one()
    for variant in VARIANTS:
        found = _db.session.query(User).filter_by(username=variant.lower()).first()
        assert found is not None and found.id == alice.id, f"{variant!r} under {collation}"


def test_an_accented_variant_cannot_authenticate_as_the_ascii_account(db):
    alice = make_user(username="alice", password=PASSWORD)
    for variant in VARIANTS:
        assert authenticate_user(variant, PASSWORD) is None, variant
    assert authenticate_user("alice", PASSWORD).id == alice.id
    assert authenticate_user("ALICE", PASSWORD).id == alice.id


def test_login_route_with_accented_variants_never_signs_in(client, db):
    make_user(username="alice", password=PASSWORD)
    for variant in VARIANTS:
        response = client.post("/login", data={"username": variant, "password": PASSWORD})
        assert response.status_code == 200, variant  # the generic failure page, never the 302 of a sign-in
        assert "Invalid username or password." in response.get_data(as_text=True)
    assert client.post("/login", data={"username": "alice", "password": PASSWORD}).status_code == 302
