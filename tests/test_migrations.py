"""Migration smoke tests.

The rest of the suite builds its schema with ``db.create_all()`` straight
from the models, which never executes a single Alembic revision. That gap
is how Milestone 04 shipped a migration whose ``ck_audit_logs_event_type``
allow-list was stale: every test passed, and the failure only appeared as
an ``IntegrityError`` during manual browser testing (see
docs/PROJECT_PROGRESS.md).

These tests close that specific hole without redesigning the fixtures: they
run the real migration chain against a temporary on-disk SQLite database
and compare the resulting schema against what the models describe. They are
deliberately few and slow-ish -- the point is to catch "the migrations and
the models have drifted apart", not to re-test application behaviour.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app import create_app
from app.extensions import db
from app.models.audit_log import AuditEvent


def _schema_snapshot(engine) -> dict:
    """Columns, foreign keys, indexes and CHECK constraints per table.
    ``alembic_version`` is excluded -- it exists only in a migrated
    database, never in the models."""
    inspector = sa.inspect(engine)
    snapshot = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        snapshot[table] = {
            "columns": {
                c["name"]: (str(c["type"]).upper(), c["nullable"])
                for c in inspector.get_columns(table)
            },
            "foreign_keys": sorted(
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    (fk.get("options") or {}).get("ondelete"),
                )
                for fk in inspector.get_foreign_keys(table)
            ),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"])) for i in inspector.get_indexes(table)
            ),
            "checks": sorted(
                (c["name"], " ".join(c["sqltext"].split()))
                for c in inspector.get_check_constraints(table)
            ),
        }
    return snapshot


def _migrated_app(tmp_path, name: str):
    """An app pointed at a fresh on-disk SQLite file. Alembic opens its own
    connections, so the suite's usual in-memory database cannot be used
    here -- each connection would get a different empty database."""
    database_path = (tmp_path / name).as_posix()
    application = create_app("testing")
    application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database_path}"
    return application


def test_migration_chain_runs_from_empty(tmp_path):
    application = _migrated_app(tmp_path, "chain.db")
    with application.app_context():
        upgrade()
        tables = set(sa.inspect(db.engine).get_table_names())
        assert {"users", "audit_logs", "menu_items", "orders", "order_items"} <= tables
        db.session.remove()
        db.engine.dispose()


def test_migrated_schema_matches_models(tmp_path):
    """The check that would have caught the M04 bug: a column, foreign key,
    index or CHECK constraint present in the models but not produced by the
    migrations (or vice versa) fails here."""
    migrated = _migrated_app(tmp_path, "migrated.db")
    with migrated.app_context():
        upgrade()
        from_migrations = _schema_snapshot(db.engine)
        db.session.remove()
        db.engine.dispose()

    from_models_app = _migrated_app(tmp_path, "models.db")
    with from_models_app.app_context():
        db.create_all()
        from_models = _schema_snapshot(db.engine)
        db.session.remove()
        db.engine.dispose()

    assert set(from_migrations) == set(from_models)
    for table in sorted(from_migrations):
        assert from_migrations[table] == from_models[table], f"schema drift in {table!r}"


def test_audit_event_allow_list_covers_every_enum_member(tmp_path):
    """Every ``AuditEvent`` member must satisfy the migrated CHECK
    constraint. This is the exact M04 failure, expressed directly: adding an
    enum member without widening the constraint breaks audit writes at
    runtime while `create_all()`-based tests stay green."""
    application = _migrated_app(tmp_path, "audit.db")
    with application.app_context():
        upgrade()
        checks = sa.inspect(db.engine).get_check_constraints("audit_logs")
        allow_list = next(
            c["sqltext"] for c in checks if c["name"] == "ck_audit_logs_event_type"
        )
        missing = [e.value for e in AuditEvent if f"'{e.value}'" not in allow_list]
        assert not missing, f"AuditEvent members absent from the migrated CHECK: {missing}"
        db.session.remove()
        db.engine.dispose()


def test_downgrade_and_reupgrade_are_reversible(tmp_path):
    if os.environ.get("TEST_DATABASE_URL", "").startswith("mysql"):
        pytest.skip(
            "known issue #32: the pre-M10 revisions abf997064564, 8d5ba0171efe and d8f3bfd0e2a9 drop "
            "foreign-key-backed indexes before their tables in downgrade(), which MySQL refuses (1553). "
            "M10's own round trip is covered on MySQL by test_ops_migration."
        )
    application = _migrated_app(tmp_path, "roundtrip.db")
    with application.app_context():
        upgrade()
        after_first = _schema_snapshot(db.engine)

        downgrade(revision="base")
        assert not {"orders", "order_items", "users"} & set(
            sa.inspect(db.engine).get_table_names()
        )

        upgrade()
        assert _schema_snapshot(db.engine) == after_first
        db.session.remove()
        db.engine.dispose()
