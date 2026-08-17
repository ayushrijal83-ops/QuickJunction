"""Flask extension instances.

Extensions are created here, unbound, and attached to an application inside
``create_app``. Keeping them in their own module is what lets models, routes
and services import ``db`` without importing the application factory.
"""

from __future__ import annotations

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite silently ignores FK constraints (RESTRICT, CASCADE, NOT NULL
    via FK) unless a connection turns them on -- unlike MySQL/InnoDB, which
    enforces them by default. Without this, the test suite's SQLite database
    would pass tests that a real MySQL deployment could fail differently on.
    No-op for every other dialect (checked via module name, not an import,
    so this file doesn't gain a driver-specific dependency)."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
