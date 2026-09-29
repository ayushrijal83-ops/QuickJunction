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

    # --- local LLM (M07) --------------------------------------------------
    # Explanation-only; the deterministic recommendation engine stays
    # authoritative (docs/AI.md). Paths are configuration, never request
    # input -- nothing in app/services/local_llm.py accepts a path from a
    # caller. Inference is local and offline; there is no API key here
    # because there is no external service.
    LLM_ENABLED: bool = _env_bool("LLM_ENABLED", True)
    LLM_MODEL_PATH: str = os.environ.get("LLM_MODEL_PATH", str(BASE_DIR / "models" / "Qwen3-0.6B-Base"))
    # LoRA adapter produced by training/train_lora.py.
    #
    # Defaults to the M07.5 (v4) adapter, promoted in M07.7 after it scored
    # 25/25 on the held-out production evaluation with every failure class at
    # zero -- against v2's 19/25, which still carried 3 hallucinations, 3
    # overclaims and a dietary-compatibility error under the same conditions
    # (see docs/AI.md §18).
    #
    # Promotion depended on the deterministic preference-safety guard in
    # app/services/local_llm.py: v4 alone scored 23/25 and still credited the
    # customer with preferences they had not set. The guard closes that class
    # outright, so it is not optional for this adapter.
    #
    # The v1, v2 and v3 adapters are retained on disk and can be selected by
    # setting this variable. If the configured directory is absent, local_llm
    # falls back to the base model rather than failing.
    LLM_ADAPTER_PATH: str | None = os.environ.get(
        "LLM_ADAPTER_PATH", str(BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora-v4")
    )
    LLM_MAX_NEW_TOKENS: int = int(os.environ.get("LLM_MAX_NEW_TOKENS", "48"))

    # Load the model on a background thread at startup instead of lazily on
    # the first explanation request. Measured: 18.3 s for the first request
    # cold, ~3.7 s warm -- so this moves a very visible wait off the user.
    #
    # Off by default here and switched on for development below, because each
    # worker process warms its own copy: N gunicorn workers means N resident
    # 1.2 GB models. That is an operator's decision, not a safe default, so
    # production must opt in explicitly after checking the memory budget.
    LLM_WARMUP: bool = _env_bool("LLM_WARMUP", False)

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
    # One process, one model: warming up here is free and removes the 18 s
    # first-request wait that a demonstration would otherwise hit.
    LLM_WARMUP: bool = _env_bool("LLM_WARMUP", True)


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
    # The 1.2 GB model must never be loaded by the ordinary test suite --
    # tests that exercise inference opt in explicitly by flipping this.
    LLM_ENABLED = False


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
        if database_uses_root(uri):
            raise ConfigError(
                "DATABASE_URL connects as the MySQL root account. Use the dedicated "
                "application account instead -- see docs/MYSQL_SETUP_HANDOFF.md section 5."
            )


def database_uses_root(uri: str | None) -> bool:
    """True when the DSN's user is MySQL ``root``. Parses the URL rather than
    pattern-matching it, and never returns or logs any part of it."""
    if not uri:
        return False
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        return (make_url(uri).username or "").lower() == "root"
    except ArgumentError:
        return False


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
