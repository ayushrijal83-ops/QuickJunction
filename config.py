"""Configuration objects for Quick Junction.

Every secret and every credential is read from the process environment.
Nothing in this file may ever contain a real key, password, host or DSN --
only names of environment variables and non-sensitive defaults.

Environments
------------
development : local work, verbose logging, debug enabled
testing     : pytest, isolated throwaway database, debug disabled
production  : deployed, debug forbidden, secrets mandatory
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Loads BASE_DIR/.env when present. The file is git-ignored and is never
# created by the repository -- see .env.example for the required names.
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class BaseConfig:
    """Settings shared by every environment."""

    ENV_NAME: str = "base"

    DEBUG: bool = False
    TESTING: bool = False

    SECRET_KEY: str | None = os.environ.get("SECRET_KEY")

    # --- database -------------------------------------------------------
    # MySQL DSN, e.g. mysql+pymysql://user:password@host:3306/quick_junction
    SQLALCHEMY_DATABASE_URI: str | None = os.environ.get("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    # MySQL drops idle connections; recycle below the server's wait_timeout.
    SQLALCHEMY_ENGINE_OPTIONS: dict = {"pool_pre_ping": True, "pool_recycle": 280}

    # --- session / cookie hardening -------------------------------------
    SESSION_COOKIE_NAME: str = "qj_session"
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"
    SESSION_COOKIE_SECURE: bool = True  # relaxed only in development
    PERMANENT_SESSION_LIFETIME: timedelta = timedelta(hours=8)

    # --- request limits --------------------------------------------------
    MAX_CONTENT_LENGTH: int = 2 * 1024 * 1024  # 2 MiB

    # --- CSRF ------------------------------------------------------------
    # Honoured by Flask-WTF, which is added in the milestone that introduces
    # the first HTML form. Kept here so the switch already has a home.
    WTF_CSRF_ENABLED: bool = True
    WTF_CSRF_TIME_LIMIT: int | None = None

    # --- errors / logging -------------------------------------------------
    # Never re-raise handled exceptions to the client.
    PROPAGATE_EXCEPTIONS: bool = False
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    LOG_DIR: Path = BASE_DIR / "logs"
    LOG_TO_FILE: bool = True

    @classmethod
    def validate(cls) -> None:
        """Fail fast on a configuration that cannot be used safely."""
        missing = [
            name
            for name in ("SECRET_KEY", "SQLALCHEMY_DATABASE_URI")
            if not getattr(cls, name)
        ]
        if missing:
            env_names = {"SQLALCHEMY_DATABASE_URI": "DATABASE_URL"}
            required = ", ".join(env_names.get(n, n) for n in missing)
            raise ConfigError(
                f"{cls.__name__}: missing required environment variable(s): "
                f"{required}. Copy .env.example to .env and fill it in."
            )


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True
    # Plain HTTP on localhost, so the Secure flag would drop the cookie.
    SESSION_COOKIE_SECURE = False
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")


class TestingConfig(BaseConfig):
    ENV_NAME = "testing"
    TESTING = True
    DEBUG = False
    # Tests must never touch a real database or need a running MySQL server.
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    SECRET_KEY = os.environ.get("TEST_SECRET_KEY", "testing-only-not-a-secret")
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_ENABLED = False
    LOG_TO_FILE = False
    LOG_LEVEL = "WARNING"


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    DEBUG = False
    TESTING = False

    @classmethod
    def validate(cls) -> None:
        super().validate()
        if cls.DEBUG or cls.TESTING:
            raise ConfigError("Debug/testing mode is forbidden in production.")
        if cls.SECRET_KEY in _WEAK_SECRETS or len(cls.SECRET_KEY or "") < 32:
            raise ConfigError(
                "SECRET_KEY is weak or a placeholder. Generate one with: "
                "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        uri = cls.SQLALCHEMY_DATABASE_URI or ""
        if uri.startswith("sqlite"):
            raise ConfigError("Production requires the MySQL DATABASE_URL.")


_WEAK_SECRETS = {
    None,
    "",
    "change-me",
    "changeme",
    "secret",
    "dev",
    "development",
    "testing-only-not-a-secret",
    "replace-with-a-64-char-random-string",
}

CONFIGS: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}

DEFAULT_CONFIG = "development"


def get_config(name: str | None = None) -> type[BaseConfig]:
    """Resolve a config class by name, falling back to ``APP_ENV``."""
    key = (name or os.environ.get("APP_ENV") or DEFAULT_CONFIG).strip().lower()
    try:
        return CONFIGS[key]
    except KeyError:
        raise ConfigError(
            f"Unknown configuration {key!r}. Expected one of: "
            f"{', '.join(sorted(CONFIGS))}."
        ) from None
