"""O1: the installation identity survives a real backup -> restore.

scripts/backup_database.py dumps the whole database, so the three identity
tables are included with no change to the backup tools. This proves it for
real: the dump contains the identity rows, and a restore brings back the
exact same UUIDs, which ``get_installation()`` then reads unchanged.

MySQL only (``mysqldump``/``mysql`` against TEST_DATABASE_URL), following
tests/test_payment_ledger_backup_restore_drill.py: wipe the same disposable
``*_test`` database, then restore into it. CI's MySQL job runs this file on
its own and fails unless the test passes rather than skips.
"""

from __future__ import annotations

import os

import pymysql
import pytest
from sqlalchemy.engine import make_url

import scripts.backup_database as bd
import scripts.restore_database as rd
from app.extensions import db as _db
from app.models.installation import Branch, Installation, Organization
from app.services.installation import get_installation
from tests.conftest import assert_disposable_test_database, wipe_mysql_schema


@pytest.fixture
def mysql_test_url(app):
    uri = os.environ.get("TEST_DATABASE_URL", "")
    if not uri.startswith("mysql"):
        pytest.skip("installation backup/restore drill needs TEST_DATABASE_URL=mysql+pymysql://...*_test")
    assert_disposable_test_database(uri)
    return uri


def _identity_rows(url):
    conn = pymysql.connect(host=url.host, port=url.port or 3306, user=url.username,
                           password=url.password or "", database=url.database)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, public_id, name, hq_id, created_at FROM organizations ORDER BY id")
            organizations = cur.fetchall()
            cur.execute("SELECT id, organization_id, public_id, name, hq_id, created_at FROM branches ORDER BY id")
            branches = cur.fetchall()
            cur.execute("SELECT id, public_id, branch_id, created_at FROM installation ORDER BY id")
            installation = cur.fetchall()
        return organizations, branches, installation
    finally:
        conn.close()


def test_backup_and_restore_preserve_the_exact_identity(mysql_test_url, db, tmp_path):
    organization = Organization(name="Cafe Himalaya")
    branch = Branch(organization=organization, name="Cafe Himalaya")
    _db.session.add_all([organization, branch, Installation(id=1, branch=branch)])
    _db.session.commit()
    before = get_installation()
    url = make_url(mysql_test_url)
    rows_before = _identity_rows(url)

    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(database_url=mysql_test_url, backup_dir=backup_dir, key_file=None,
                            encrypt=False, retention=7)
    assert {t: meta.table_row_counts.get(t) for t in ("organizations", "branches", "installation")} == {
        "organizations": 1, "branches": 1, "installation": 1}
    dump = (backup_dir / meta.file_name).read_text(encoding="utf-8")
    for table in ("organizations", "branches", "installation"):
        assert f"CREATE TABLE `{table}`" in dump
    for public_id in (before.organization_public_id, before.branch_public_id, before.installation_public_id):
        assert public_id in dump

    # Total loss of this disposable database, then a real restore into it.
    # End the session's open read transaction first, or its metadata locks
    # block the DROP TABLEs (see the payment-ledger drill).
    _db.session.rollback()
    _db.session.remove()
    _db.engine.dispose()
    wipe_mysql_schema(mysql_test_url)
    results = rd.restore_backup(backup_path=backup_dir / meta.file_name, database_url=mysql_test_url,
                                target=url.database, key_file=None)
    for table in ("organizations", "branches", "installation"):
        assert results[table] == (1, 1)

    assert _identity_rows(url) == rows_before  # same ids, UUIDs, names, links and timestamps
    _db.engine.dispose()  # no pooled connection from before the restore
    after = get_installation()
    assert after == before
    assert get_installation() == before  # and reading it again changes nothing
    assert _identity_rows(url) == rows_before
