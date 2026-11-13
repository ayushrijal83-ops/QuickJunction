"""M5 migrations (b1c4e7f9a2d5, c2d5f8a1b3e6) against a database that already
holds menu data: upgrade, downgrade back to M4's head, upgrade again.

Runs on the TEST_DATABASE_URL MySQL database when one is set (conftest wipes
it first, because this module's name contains "migration"), otherwise on a
scratch SQLite file. Only the M5 revisions are downgraded, so known issue
#32 (full downgrade to base on MySQL) does not apply.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app import create_app
from app.extensions import db

M4_HEAD = "e8b4d2a6c913"
NEPALI = "पनिर टिक्का"


def _app(tmp_path):
    application = create_app("testing")
    if not os.environ.get("TEST_DATABASE_URL", "").startswith("mysql"):
        application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / 'm5.db').as_posix()}"
    return application


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(db.engine).get_columns(table)}


def test_m5_upgrade_downgrade_upgrade_keeps_existing_menu(tmp_path):
    application = _app(tmp_path)
    with application.app_context():
        try:
            upgrade(revision=M4_HEAD)
            with db.engine.begin() as conn:
                conn.execute(sa.text("INSERT INTO categories (id, name, is_active, created_at, updated_at)"
                                     " VALUES (1, 'Starters', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
                conn.execute(sa.text(
                    "INSERT INTO menu_items (id, category_id, name, price, cuisine, spice_level, dietary_type,"
                    " is_available, created_at, updated_at) VALUES (1, 1, 'Paneer Tikka', 199.00, 'indian', 'medium',"
                    " 'vegetarian', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))

            upgrade()
            with db.engine.begin() as conn:
                assert conn.execute(sa.text("SELECT id, name, default_locale FROM restaurant_profile")).all() == [
                    (1, "Quick Junction", "en")]
                # No invented restaurant details on a real installation.
                assert conn.execute(sa.text("SELECT tagline, address, phone, email FROM restaurant_profile")).one() == (
                    None, None, None, None)
                assert conn.execute(sa.text("SELECT sort_order FROM categories")).scalar() == 0
                assert conn.execute(sa.text("SELECT sort_order FROM menu_items")).scalar() == 0
                conn.execute(sa.text("INSERT INTO menu_item_translations (menu_item_id, locale, name, created_at,"
                                     " updated_at) VALUES (1, 'ne', :n, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                             {"n": NEPALI})
                assert conn.execute(sa.text("SELECT name FROM menu_item_translations")).scalar() == NEPALI
                conn.execute(sa.text("INSERT INTO audit_logs (event_type, success, created_at)"
                                     " VALUES ('restaurant_profile_changed', 1, CURRENT_TIMESTAMP)"))

            downgrade(revision=M4_HEAD)
            tables = set(sa.inspect(db.engine).get_table_names())
            assert not {"restaurant_profile", "category_translations", "menu_item_translations"} & tables
            assert "sort_order" not in _columns("categories") and "sort_order" not in _columns("menu_items")
            with db.engine.connect() as conn:
                assert conn.execute(sa.text("SELECT name FROM menu_items")).scalar() == "Paneer Tikka"  # kept
                assert conn.execute(sa.text("SELECT COUNT(*) FROM audit_logs"
                                            " WHERE event_type = 'restaurant_profile_changed'")).scalar() == 0

            upgrade()
            with db.engine.connect() as conn:
                assert conn.execute(sa.text("SELECT COUNT(*) FROM restaurant_profile")).scalar() == 1
                # Tracks whatever the current head is, not literally M5's own
                # revision -- update this each time a later migration lands.
                assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == "4c7e2a9f1b38"
        finally:
            # This test's own fixed-id inserts (categories.id=1,
            # menu_items.id=1) are the only rows it can leave behind -- the
            # autouse migration-schema wipe in conftest guarantees no id=1
            # row existed before it ran. A fresh connection (not one left
            # over from a failed assertion above) deletes them in FK order
            # so an opt-in MySQL run's shared *_test database stays clean
            # for the next, non-migration test even when this test itself
            # fails. The table-existence check guards a failure early
            # enough that these tables were never (re)created, without
            # weakening cleanup when they are present.
            existing_tables = set(sa.inspect(db.engine).get_table_names())
            with db.engine.begin() as conn:
                if "menu_items" in existing_tables:
                    conn.execute(sa.text("DELETE FROM menu_items WHERE id = 1"))
                if "categories" in existing_tables:
                    conn.execute(sa.text("DELETE FROM categories WHERE id = 1"))
            db.session.remove()
            db.engine.dispose()
