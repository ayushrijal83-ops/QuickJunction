"""Phase 3: the users.staff_approved migration against pre-existing rows,
and the guard against running the application as MySQL root."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app import create_app
from app.extensions import db
from config import ConfigError, ProductionConfig, database_uses_root


def test_existing_staff_are_grandfathered_and_others_untouched(tmp_path):
    application = create_app("testing")
    application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    with application.app_context():
        upgrade(revision="2d9f3b20045f")  # the schema before Phase 3
        with db.engine.begin() as conn:
            for name, role in (("cust", "customer"), ("cook", "staff"), ("boss", "admin")):
                conn.execute(sa.text(
                    "INSERT INTO users (username, email, password_hash, role, is_active, session_version,"
                    " created_at, updated_at) VALUES (:n, :e, 'x', :r, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"n": name, "e": f"{name}@example.com", "r": role})

        upgrade()
        with db.engine.connect() as conn:
            rows = dict(conn.execute(sa.text("SELECT username, staff_approved FROM users")).all())
            assert rows == {"cust": 0, "cook": 1, "boss": 0}
            # New rows default to pending at the database level too.
            conn.execute(sa.text(
                "INSERT INTO users (username, email, password_hash, role, is_active, session_version,"
                " created_at, updated_at) VALUES ('new', 'new@example.com', 'x', 'staff', 1, 0,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            assert conn.execute(sa.text("SELECT staff_approved FROM users WHERE username='new'")).scalar() == 0
            conn.rollback()

        downgrade(revision="2d9f3b20045f")
        columns = {c["name"] for c in sa.inspect(db.engine).get_columns("users")}
        assert "staff_approved" not in columns
        with db.engine.connect() as conn:
            assert conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() == 3  # no data lost
        db.session.remove()
        db.engine.dispose()


@pytest.mark.parametrize("uri, expected", [
    ("mysql+pymysql://root:pw@127.0.0.1:3306/quick_junction", True),
    ("mysql+pymysql://ROOT:pw@127.0.0.1/quick_junction", True),
    ("mysql+pymysql://qj_user:pw@127.0.0.1:3306/quick_junction", False),
    ("sqlite://", False),
    (None, False),
    ("not a url", False),
])
def test_database_uses_root(uri, expected):
    assert database_uses_root(uri) is expected


def test_production_refuses_root_without_echoing_the_url():
    class RootProduction(ProductionConfig):
        SECRET_KEY = "x" * 64
        SQLALCHEMY_DATABASE_URI = "mysql+pymysql://root:do-not-print-me@127.0.0.1/quick_junction"

    with pytest.raises(ConfigError) as excinfo:
        RootProduction.validate()
    assert "root" in str(excinfo.value)
    assert "do-not-print-me" not in str(excinfo.value)


def test_development_warns_about_root(monkeypatch, caplog):
    import logging

    from config import TestingConfig

    # Alembic's fileConfig (run by any earlier migration test) disables
    # loggers that already exist, including this one.
    monkeypatch.setattr(logging.getLogger("app"), "disabled", False)
    monkeypatch.setattr(TestingConfig, "SQLALCHEMY_DATABASE_URI",
                        "mysql+pymysql://root:do-not-print-me@127.0.0.1/quick_junction")
    with caplog.at_level("WARNING"):
        create_app("testing")
    assert any("MySQL 'root'" in r.getMessage() for r in caplog.records)
    assert "do-not-print-me" not in caplog.text
