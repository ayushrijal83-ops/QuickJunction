"""Milestone 01 foundation tests.

Covers the six guarantees the milestone claims: the app builds, each
configuration behaves, the health endpoint answers, the database is
configured, secrets stay out of responses, and production cannot run with
debug on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest
from flask import Flask

from app import create_app
from config import ConfigError, DevelopmentConfig, ProductionConfig, TestingConfig


# 1. Application can be created ------------------------------------------


def test_application_factory_returns_configured_app(app: Flask) -> None:
    assert isinstance(app, Flask)
    assert app.config["SECRET_KEY"]
    assert "health.health" in app.view_functions


def test_unknown_configuration_name_is_rejected() -> None:
    with pytest.raises(ConfigError):
        create_app("staging")


# 2. Testing configuration works -----------------------------------------


def test_testing_configuration(app: Flask) -> None:
    assert app.config["TESTING"] is True
    assert app.config["DEBUG"] is False
    assert app.config["ENV_NAME"] == "testing"
    # Tests must never write to a log file or hit a real database. SQLite by
    # default; an opt-in MySQL run (TEST_DATABASE_URL, M10 verification) must
    # use a disposable *_test database, never the application's own.
    assert app.config["LOG_TO_FILE"] is False
    from tests.conftest import assert_disposable_test_database

    assert_disposable_test_database(app.config["SQLALCHEMY_DATABASE_URI"])
    if "TEST_DATABASE_URL" not in os.environ:
        assert "mysql" not in app.config["SQLALCHEMY_DATABASE_URI"]


# 3. Health endpoint works -------------------------------------------------


def test_health_endpoint_returns_exactly_status_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {"status": "ok"}


# 4. Database configuration loads correctly --------------------------------


def test_database_configuration_loads(app: Flask) -> None:
    from app.extensions import db

    assert app.config["SQLALCHEMY_DATABASE_URI"]
    assert app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False
    with app.app_context():
        assert db.engine is not None
        # Milestone 02 added auth/audit; Milestone 03 added the menu data
        # layer; Milestone 04 added cart/order placement/order history;
        # Milestone 10 added restaurant tables and reservations; Milestone 11
        # payments; Milestone 12 pricing settings; Milestone 13 stock movements;
        # M4 one-time account codes; M5 the restaurant profile and menu translations.
        assert set(db.metadata.tables) == {
            "account_tokens",
            "restaurant_profile",
            "category_translations",
            "menu_item_translations",
            "users",
            "audit_logs",
            "categories",
            "menu_items",
            "ingredients",
            "menu_item_ingredients",
            "customer_preferences",
            "orders",
            "order_items",
            "restaurant_tables",
            "reservations",
            "payments",
            "pricing_settings",
            "stock_movements",
        }


@pytest.mark.parametrize("uri", ["sqlite://", "sqlite:///:memory:", "sqlite:///file::memory:?cache=shared",
                                 "sqlite:///x.db?mode=memory"])
def test_development_refuses_an_in_memory_database(monkeypatch, uri) -> None:
    """A DATABASE_URL left in the shell overrides .env; an in-memory one used
    to start a server on an empty database that failed every page with
    "no such table". It now stops at startup with a safe message."""
    monkeypatch.setattr(DevelopmentConfig, "SECRET_KEY", "dev-secret")
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", uri)
    with pytest.raises(ConfigError, match="in-memory SQLite") as refused:
        create_app("development")
    assert "Remove-Item Env:DATABASE_URL" in str(refused.value) and uri not in str(refused.value)


@pytest.mark.parametrize("uri", ["mysql+pymysql://qj_user:pw@127.0.0.1:3306/quickjunctiondb", "sqlite:///dev.db"])
def test_development_accepts_a_persistent_database(monkeypatch, uri) -> None:
    monkeypatch.setattr(DevelopmentConfig, "SECRET_KEY", "dev-secret")
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", uri)
    DevelopmentConfig.validate()  # a deliberately supplied MySQL or file URL still wins


def test_invalid_database_url_fails_without_echoing_it(monkeypatch) -> None:
    monkeypatch.setattr(DevelopmentConfig, "SECRET_KEY", "dev-secret")
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", "not a url s3cr3t-password")
    with pytest.raises(ConfigError, match="not a valid database URL") as refused:
        DevelopmentConfig.validate()
    assert "s3cr3t-password" not in str(refused.value)


def test_password_typed_into_the_port_fails_safely(monkeypatch) -> None:
    """A swapped ':'/'@' puts the password where the port goes; SQLAlchemy
    then raises ValueError quoting it. Startup must stop with the safe
    ConfigError instead, with the secret nowhere in the traceback."""
    secret = "qj-test-secret-7f3a9c"
    monkeypatch.setattr(DevelopmentConfig, "SECRET_KEY", "dev-secret")
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI",
                        f"mysql+pymysql://qj_user@127.0.0.1:{secret}/quickjunctiondb")
    with pytest.raises(ConfigError, match="not a valid database URL") as refused:
        create_app("development")
    error = refused.value
    formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert secret not in str(error) and secret not in formatted
    assert error.__context__ is None and error.__cause__ is None


def test_environment_database_url_takes_precedence_over_dotenv(tmp_path) -> None:
    """The precedence behind the incident: .env never overrides a variable
    already in the process environment. Run in a fresh interpreter so the
    module-level config is read from that environment; only True/False is
    printed, never the URL. Holds whether or not a .env file exists."""
    sentinel = f"sqlite:///{(tmp_path / 'sentinel.db').as_posix()}"
    env = {**os.environ, "DATABASE_URL": sentinel, "QJ_EXPECTED_URI": sentinel}
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, config; print(config.DevelopmentConfig.SQLALCHEMY_DATABASE_URI == os.environ['QJ_EXPECTED_URI'])"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0 and result.stdout.strip() == "True"
    assert not (tmp_path / "sentinel.db").exists()  # importing config opens no database


def test_testing_keeps_its_in_memory_database(monkeypatch) -> None:
    """In-memory SQLite stays valid for tests; only development refuses it."""
    monkeypatch.setattr(TestingConfig, "SQLALCHEMY_DATABASE_URI", "sqlite://")
    TestingConfig.validate()
    assert create_app("testing").config["SQLALCHEMY_DATABASE_URI"] == "sqlite://"

    monkeypatch.setattr(DevelopmentConfig, "SECRET_KEY", "dev-secret")
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", "sqlite://")
    with pytest.raises(ConfigError, match="in-memory SQLite"):
        DevelopmentConfig.validate()


def test_production_database_must_be_mysql(monkeypatch) -> None:
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "x" * 64)
    monkeypatch.setattr(ProductionConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///x.db")
    with pytest.raises(ConfigError):
        ProductionConfig.validate()


# 5. Application does not expose secrets ------------------------------------


def test_responses_never_leak_secrets_or_internals(app: Flask, client) -> None:
    secret = app.config["SECRET_KEY"]
    database_uri = app.config["SQLALCHEMY_DATABASE_URI"]

    @app.get("/_boom")
    def boom():
        raise RuntimeError(f"leaky {secret} {database_uri}")

    responses = [
        client.get("/health"),
        client.get("/does-not-exist"),
        client.post("/health"),  # 405
        client.get("/_boom"),
    ]

    for response in responses:
        body = response.get_data(as_text=True)
        assert secret not in body
        assert database_uri not in body
        for leak in ("Traceback", "SECRET_KEY", "DATABASE_URL", "site-packages"):
            assert leak not in body, f"{leak!r} leaked in {response.status}"

    assert responses[-1].status_code == 500
    assert responses[-1].get_json()["error"]["message"] == "An internal error occurred."


def test_no_secret_is_hardcoded_in_configuration() -> None:
    # Real environments read their secret from the environment, nowhere else.
    assert DevelopmentConfig.SECRET_KEY == os.environ.get("SECRET_KEY")
    assert ProductionConfig.SQLALCHEMY_DATABASE_URI == os.environ.get("DATABASE_URL")
    # The one literal in the tree is the testing placeholder...
    assert TestingConfig.SECRET_KEY == "testing-only-not-a-secret"
    with pytest.raises(ConfigError):
        # ...and it must never satisfy production.
        # A placeholder secret must never satisfy production.
        type(
            "WeakProd",
            (ProductionConfig,),
            {
                "SECRET_KEY": "testing-only-not-a-secret",
                "SQLALCHEMY_DATABASE_URI": "mysql+pymysql://user:pw@host/db",
            },
        ).validate()



# 6. Production configuration disables debug ---------------------------------


def test_production_disables_debug_and_hardens_session() -> None:
    assert ProductionConfig.DEBUG is False
    assert ProductionConfig.TESTING is False
    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.SESSION_COOKIE_HTTPONLY is True
    assert ProductionConfig.SESSION_COOKIE_SAMESITE == "Lax"
    assert ProductionConfig.PROPAGATE_EXCEPTIONS is False


def test_production_rejects_missing_secrets(monkeypatch) -> None:
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", None)
    monkeypatch.setattr(ProductionConfig, "SQLALCHEMY_DATABASE_URI", None)
    with pytest.raises(ConfigError):
        create_app("production")
