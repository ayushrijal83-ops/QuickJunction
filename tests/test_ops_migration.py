"""Milestone 10: b5e2f8c41a07 against a database that already holds orders."""

from __future__ import annotations

import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app import create_app
from app.extensions import db
from tests.conftest import rejected_by_check_constraint


def test_existing_orders_backfilled_and_downgrade_keeps_them(tmp_path):
    application = create_app("testing")
    application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / 'ops.db').as_posix()}"
    with application.app_context():
        upgrade(revision="7c4e1a9b52d3")  # the schema before this milestone
        with db.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO users (id, username, email, password_hash, role, is_active, session_version,"
                " staff_approved, created_at, updated_at) VALUES (1, 'old', 'old@example.com', 'x', 'customer',"
                " 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            for status in ("completed", "cancelled"):
                conn.execute(sa.text(
                    "INSERT INTO orders (user_id, status, subtotal, total, created_at, updated_at)"
                    " VALUES (1, :s, 250.00, 250.00, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"s": status})

        upgrade()
        with db.engine.begin() as conn:
            rows = conn.execute(sa.text(
                "SELECT status, source, table_id, cancellation_actor, subtotal FROM orders ORDER BY id")).all()
            assert [tuple(r) for r in rows] == [
                ("completed", "online", None, None, 250), ("cancelled", "online", None, None, 250)]
            # The database itself now refuses a dine-in order without a table.
            with rejected_by_check_constraint():
                conn.execute(sa.text("UPDATE orders SET source = 'dine_in' WHERE id = 1"))
        with db.engine.begin() as conn:
            conn.execute(sa.text("UPDATE orders SET status = 'served' WHERE id = 1"))  # new status accepted
        assert "reservations" in sa.inspect(db.engine).get_table_names()

        downgrade(revision="7c4e1a9b52d3")
        inspector = sa.inspect(db.engine)
        assert "restaurant_tables" not in inspector.get_table_names()
        assert "source" not in {c["name"] for c in inspector.get_columns("orders")}
        with db.engine.connect() as conn:
            assert conn.execute(sa.text("SELECT COUNT(*) FROM orders")).scalar() == 2  # no data lost
            # 'served' cannot exist under the old CHECK; it maps back to 'ready'.
            assert conn.execute(sa.text("SELECT status FROM orders WHERE id = 1")).scalar() == "ready"
        db.session.remove()
        db.engine.dispose()
