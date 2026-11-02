"""M7.2 -- backup and restore (scripts/backup_database.py, restore_database.py).

Everything except test_live_backup_and_restore_drill runs with no MySQL
server: the mysqldump/mysql subprocess calls are monkeypatched out, so these
tests exercise the real metadata, checksum, encryption, retention and
safety-gate logic without touching a database at all.

test_live_backup_and_restore_drill is the real article: it runs only when
TEST_DATABASE_URL points at a disposable `*_test` MySQL database (the same
convention tests/test_mysql_concurrency.py uses), and is skipped otherwise.
It creates a small table, backs it up, drops the table (simulating data
loss), restores the backup, and verifies the data is back byte-for-byte --
not just that the restore command returned 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pymysql
import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.backup_database as bd  # noqa: E402
import scripts.restore_database as rd  # noqa: E402
from scripts._backup_common import (  # noqa: E402
    BackupError,
    BackupMetadata,
    DUMP_COMPLETE_MARKER,
    DUMP_HEADER_MARKER,
    mysql_defaults_file,
    quote_mysql_option_value,
    sha256_file,
    validate_database_name,
)
from scripts._backup_crypto import generate_key_file, key_fingerprint, load_key  # noqa: E402
from tests.conftest import assert_disposable_test_database  # noqa: E402


class _FakeCursor:
    """Stands in for a pymysql cursor against a fake connection whose only
    job is to answer SHOW TABLES (for ensure_target_is_empty) and, optionally,
    raise on a specific table's SELECT COUNT(*) (for verify_restored_data)."""

    def __init__(self, tables, raise_for_table=None):
        self._tables = tables
        self._raise_for_table = raise_for_table
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, *args):
        self._last_sql = sql
        if self._raise_for_table and f"`{self._raise_for_table}`" in sql:
            raise pymysql.err.OperationalError(1146, f"Table '{self._raise_for_table}' doesn't exist")

    def fetchall(self):
        if "SHOW TABLES" in self._last_sql:
            return [(t,) for t in self._tables]
        return []

    def fetchone(self):
        return (0,)


class _FakeConnection:
    def __init__(self, tables=(), raise_for_table=None):
        self._tables = tables
        self._raise_for_table = raise_for_table

    def cursor(self):
        return _FakeCursor(self._tables, self._raise_for_table)

    def close(self):
        pass

FAKE_DUMP = (
    f"{DUMP_HEADER_MARKER} 10.13  Distrib 8.0.46, for Win64\n"
    "CREATE TABLE `foo` (`id` int NOT NULL);\n"
    "INSERT INTO `foo` VALUES (1),(2);\n"
    f"{DUMP_COMPLETE_MARKER} 2026-11-01 12:00:00\n"
).encode("utf-8")


def _fake_run_mysqldump(mysqldump_path, url, out_path):
    out_path.write_bytes(FAKE_DUMP)


def _patch_backup_io(monkeypatch, row_counts=None):
    monkeypatch.setattr(bd, "find_mysql_binary", lambda name: "fake-mysqldump")
    monkeypatch.setattr(bd, "run_mysqldump", _fake_run_mysqldump)
    monkeypatch.setattr(bd, "table_row_counts", lambda url: row_counts or {"foo": 2})
    monkeypatch.setattr(bd, "mysqldump_version", lambda path: "fake mysqldump 8.0.46")


# =============================================================================
# metadata and checksum
# =============================================================================


def test_backup_metadata_and_checksum(tmp_path, monkeypatch):
    _patch_backup_io(monkeypatch)
    key_path = tmp_path / "k.key"
    generate_key_file(key_path)
    backup_dir = tmp_path / "backups"

    meta = bd.create_backup(
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=key_path, encrypt=True, retention=7,
    )

    assert meta.database == "fake_test"
    assert meta.host == "127.0.0.1"
    assert meta.port == 3306
    assert meta.encrypted is True
    assert meta.table_row_counts == {"foo": 2}
    assert meta.tool_version

    data_path = backup_dir / meta.file_name
    assert data_path.is_file()
    assert data_path.suffix == ".enc"
    assert sha256_file(data_path) == meta.sha256, "recorded checksum must match the actual file"

    meta_files = list(backup_dir.glob("*.json"))
    assert len(meta_files) == 1
    round_tripped = BackupMetadata.from_json(meta_files[0].read_text(encoding="utf-8"))
    assert round_tripped == meta

    # the data on disk is genuinely encrypted -- not the plaintext dump
    assert FAKE_DUMP not in data_path.read_bytes()


def test_unencrypted_backup_contains_plaintext_but_is_still_checksummed(tmp_path, monkeypatch):
    _patch_backup_io(monkeypatch)
    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=None, encrypt=False, retention=7,
    )
    assert meta.encrypted is False
    assert meta.key_fingerprint is None
    data_path = backup_dir / meta.file_name
    assert data_path.read_bytes() == FAKE_DUMP
    assert sha256_file(data_path) == meta.sha256


# =============================================================================
# mysqldump command construction
# =============================================================================


def test_mysqldump_command_includes_skip_add_drop_table(tmp_path, monkeypatch):
    """New backups must not contain DROP TABLE statements -- the real
    safety net against overwriting an existing table is the empty-target
    check in restore_database.py, but this is a second, independent layer:
    without it, mysqldump's --opt default (--add-drop-table on) would make
    a restore into a same-named table silently replace it instead of
    failing with a plain MySQL error."""
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0
            stderr = b""

        return _Result()

    monkeypatch.setattr(bd.subprocess, "run", _fake_run)
    url = make_url("mysql+pymysql://u:p@127.0.0.1:3306/fake_test")
    bd.run_mysqldump("fake-mysqldump", url, tmp_path / "dump.sql")

    assert "--skip-add-drop-table" in captured["cmd"]


# =============================================================================
# incomplete / corrupted backup detection
# =============================================================================


def test_incomplete_dump_is_rejected(tmp_path):
    truncated = tmp_path / "truncated.sql"
    truncated.write_bytes(f"{DUMP_HEADER_MARKER} 10.13\nCREATE TABLE foo (id int);\n".encode())
    with pytest.raises(BackupError, match="truncated"):
        bd.verify_dump_complete(truncated)


def test_empty_dump_is_rejected(tmp_path):
    empty = tmp_path / "empty.sql"
    empty.write_bytes(b"")
    with pytest.raises(BackupError, match="empty"):
        bd.verify_dump_complete(empty)


def test_dump_without_header_is_rejected(tmp_path):
    bogus = tmp_path / "bogus.sql"
    bogus.write_bytes(f"not a dump\n{DUMP_COMPLETE_MARKER} now\n".encode())
    with pytest.raises(BackupError, match="header"):
        bd.verify_dump_complete(bogus)


def test_create_backup_leaves_no_file_when_mysqldump_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "find_mysql_binary", lambda name: "fake-mysqldump")

    def _failing_dump(mysqldump_path, url, out_path):
        raise BackupError("mysqldump exited 2: Access denied")

    monkeypatch.setattr(bd, "run_mysqldump", _failing_dump)
    backup_dir = tmp_path / "backups"

    with pytest.raises(BackupError, match="Access denied"):
        bd.create_backup(
            database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
            backup_dir=backup_dir, key_file=None, encrypt=False, retention=7,
        )
    assert not backup_dir.exists() or list(backup_dir.iterdir()) == [], (
        "a failed backup must never leave a file that could be mistaken for a completed one"
    )


def test_corrupted_backup_is_detected_before_any_restore_attempt(tmp_path, monkeypatch):
    """Corrupting the data file after a successful backup must be caught by
    the checksum check, before the restore tool ever tries to connect to a
    database -- proven here by never patching ensure_target_exists and
    still getting the checksum error, not a connection error."""
    _patch_backup_io(monkeypatch)
    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=None, encrypt=False, retention=7,
    )
    data_path = backup_dir / meta.file_name
    data_path.write_bytes(data_path.read_bytes() + b"corruption")

    with pytest.raises(BackupError, match="Checksum mismatch"):
        rd.restore_backup(
            backup_path=data_path,
            database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
            target="fake_test",  # ends in _test, passes the target gate
            key_file=None,
        )


def test_missing_metadata_sidecar_is_detected(tmp_path):
    orphan = tmp_path / "orphan_test_20260101T000000.sql"
    orphan.write_bytes(FAKE_DUMP)
    with pytest.raises(BackupError, match="metadata sidecar"):
        rd.load_metadata(orphan)


# =============================================================================
# subprocess timeout handling
# =============================================================================


def test_mysqldump_timeout_is_converted_to_backup_error(tmp_path, monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["mysqldump"], timeout=3600)

    monkeypatch.setattr(bd.subprocess, "run", _raise_timeout)
    url = make_url("mysql+pymysql://u:p@127.0.0.1:3306/fake_test")

    with pytest.raises(BackupError, match="did not finish"):
        bd.run_mysqldump("fake-mysqldump", url, tmp_path / "dump.sql")


def test_mysql_restore_timeout_is_converted_to_backup_error(tmp_path, monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["mysql"], timeout=3600)

    monkeypatch.setattr(rd.subprocess, "run", _raise_timeout)
    url = make_url("mysql+pymysql://u:p@127.0.0.1:3306/fake_test")
    sql_path = tmp_path / "restore.sql"
    sql_path.write_bytes(b"SELECT 1;")

    with pytest.raises(BackupError, match="did not finish"):
        rd.run_mysql_restore("fake-mysql", url, sql_path)


# =============================================================================
# MySQL option-file password safety
# =============================================================================


def test_mysql_defaults_file_quotes_special_characters_in_password():
    password = 'p@ss#word "with" \\slashes and spaces'
    with mysql_defaults_file("localhost", 3306, "qj_user", password) as path:
        content = Path(path).read_text(encoding="utf-8")

    # '#' must not start a comment that truncates the rest of the value,
    # and an embedded '"' / '\' must not end the quoted value early.
    assert 'password="p@ss#word \\"with\\" \\\\slashes and spaces"\n' in content
    assert "p@ss#word" in content.split("password=", 1)[1]


@pytest.mark.parametrize("bad_char", ["\n", "\r", "\0"])
def test_mysql_defaults_file_rejects_control_characters_in_password(bad_char):
    with pytest.raises(BackupError, match="newline or NUL"):
        with mysql_defaults_file("localhost", 3306, "qj_user", f"bad{bad_char}password"):
            pass  # pragma: no cover - must never be reached


def test_quote_mysql_option_value_round_trips_plain_password():
    assert quote_mysql_option_value("PlainPassword123") == '"PlainPassword123"'


# =============================================================================
# retention
# =============================================================================


def _make_fake_backup(backup_dir: Path, database: str, seq: int) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{database}_2026010{seq}T000000"
    data = backup_dir / f"{stem}.sql"
    data.write_bytes(FAKE_DUMP)
    meta = BackupMetadata(
        tool_version="1.0.0", created_at=f"2026-01-0{seq}T00:00:00Z", database=database,
        host="127.0.0.1", port=3306, file_name=data.name, file_size_bytes=len(FAKE_DUMP),
        sha256=sha256_file(data), encrypted=False, key_fingerprint=None,
        mysqldump_version=None, table_row_counts={},
    )
    (backup_dir / f"{stem}.json").write_text(meta.to_json(), encoding="utf-8")


def test_retention_keeps_newest_and_deletes_oldest(tmp_path):
    backup_dir = tmp_path / "backups"
    for seq in range(1, 6):  # 5 backups
        _make_fake_backup(backup_dir, "fake_test", seq)

    bd.apply_retention(backup_dir, "fake_test", retention=2)

    remaining = sorted(p.stem for p in backup_dir.glob("*.json"))
    assert len(remaining) == 2
    assert "fake_test_20260105T000000" in remaining  # newest kept
    assert "fake_test_20260101T000000" not in remaining  # oldest deleted


def test_retention_never_deletes_down_to_zero(tmp_path):
    backup_dir = tmp_path / "backups"
    for seq in range(1, 4):
        _make_fake_backup(backup_dir, "fake_test", seq)

    bd.apply_retention(backup_dir, "fake_test", retention=0)

    remaining = list(backup_dir.glob("*.json"))
    assert len(remaining) == 1, "retention must never delete the only usable backup"


def test_retention_only_touches_backups_of_the_named_database(tmp_path):
    backup_dir = tmp_path / "backups"
    for seq in range(1, 4):
        _make_fake_backup(backup_dir, "fake_test", seq)
    _make_fake_backup(backup_dir, "other_test", 1)

    bd.apply_retention(backup_dir, "fake_test", retention=1)

    assert (backup_dir / "other_test_20260101T000000.json").exists()


# =============================================================================
# unsafe restore target rejection
# =============================================================================


@pytest.mark.parametrize("schema", ["mysql", "sys", "information_schema", "performance_schema"])
def test_restore_refuses_system_schemas(schema):
    with pytest.raises(BackupError):
        rd.verify_target_allowed(schema, allow_production_target=True, confirm=f"RESTORE {schema}")


def test_restore_refuses_invalid_identifier():
    with pytest.raises(BackupError):
        rd.verify_target_allowed("not a valid name; DROP TABLE x", allow_production_target=True, confirm=None)


def test_restore_allows_test_suffixed_target_with_no_flags():
    rd.verify_target_allowed("quick_junction_test", allow_production_target=False, confirm=None)  # no raise


def test_restore_refuses_non_test_target_without_confirmation():
    with pytest.raises(BackupError, match="_test"):
        rd.verify_target_allowed("quick_junction", allow_production_target=False, confirm=None)


def test_restore_refuses_non_test_target_with_flag_but_no_confirm_match():
    with pytest.raises(BackupError, match="did not match"):
        rd.verify_target_allowed("quick_junction", allow_production_target=True, confirm="yes please")


def test_restore_allows_non_test_target_with_exact_typed_confirmation():
    rd.verify_target_allowed("quick_junction", allow_production_target=True, confirm="RESTORE quick_junction")  # no raise


# =============================================================================
# restore target emptiness (ensure_target_is_empty) -- the actual fix for
# the false "restoring into a non-empty database fails safely" claim
# =============================================================================


def test_restore_refuses_non_empty_target_and_never_invokes_mysql(tmp_path, monkeypatch):
    _patch_backup_io(monkeypatch)
    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=None, encrypt=False, retention=7,
    )
    data_path = backup_dir / meta.file_name

    monkeypatch.setattr(rd, "find_mysql_binary", lambda name: "fake-mysql")
    monkeypatch.setattr(pymysql, "connect", lambda **kw: _FakeConnection(tables=["orders", "users"]))
    run_calls = []
    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: run_calls.append(a) or None)

    with pytest.raises(BackupError, match="must be completely empty"):
        rd.restore_backup(
            backup_path=data_path,
            database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
            target="fake_test", key_file=None,
        )
    assert run_calls == [], "mysql restore must never run against a non-empty target"


def test_restore_refuses_non_empty_production_target_even_with_full_confirmation(tmp_path, monkeypatch):
    """The empty-target check does not look at the target's name or trust
    --allow-production-target/--confirm -- it is unconditional."""
    _patch_backup_io(monkeypatch)
    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=None, encrypt=False, retention=7,
    )
    data_path = backup_dir / meta.file_name

    monkeypatch.setattr(rd, "find_mysql_binary", lambda name: "fake-mysql")
    monkeypatch.setattr(pymysql, "connect", lambda **kw: _FakeConnection(tables=["orders"]))
    run_calls = []
    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: run_calls.append(a) or None)

    with pytest.raises(BackupError, match="must be completely empty"):
        rd.restore_backup(
            backup_path=data_path,
            database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
            target="quick_junction", key_file=None,
            allow_production_target=True, confirm="RESTORE quick_junction",
        )
    assert run_calls == []


def test_restore_allows_empty_target_to_proceed(tmp_path, monkeypatch):
    _patch_backup_io(monkeypatch)
    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=None, encrypt=False, retention=7,
    )
    data_path = backup_dir / meta.file_name

    monkeypatch.setattr(rd, "find_mysql_binary", lambda name: "fake-mysql")
    monkeypatch.setattr(pymysql, "connect", lambda **kw: _FakeConnection(tables=[]))
    monkeypatch.setattr(rd, "verify_restored_data", lambda url, counts: {})

    run_calls = []

    def _fake_run(cmd, **kwargs):
        run_calls.append(cmd)

        class _Result:
            returncode = 0
            stderr = b""

        return _Result()

    monkeypatch.setattr(rd.subprocess, "run", _fake_run)

    rd.restore_backup(
        backup_path=data_path,
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        target="fake_test", key_file=None,
    )
    assert len(run_calls) == 1, "restore must proceed once the target is confirmed empty"


# =============================================================================
# missing / invalid encryption key
# =============================================================================


def test_create_backup_refuses_to_run_without_a_key_when_encryption_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "find_mysql_binary", lambda name: "fake-mysqldump")
    with pytest.raises(BackupError, match="Encryption is required"):
        bd.create_backup(
            database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
            backup_dir=tmp_path / "backups", key_file=None, encrypt=True, retention=7,
        )


def test_load_key_missing_file(tmp_path):
    with pytest.raises(BackupError, match="not found"):
        load_key(tmp_path / "does_not_exist.key")


def test_load_key_invalid_contents(tmp_path):
    bad = tmp_path / "bad.key"
    bad.write_bytes(b"this is not a fernet key")
    with pytest.raises(BackupError, match="valid Fernet key"):
        load_key(bad)


def test_generate_key_refuses_to_overwrite(tmp_path):
    key_path = tmp_path / "k.key"
    generate_key_file(key_path)
    with pytest.raises(BackupError, match="overwrite"):
        generate_key_file(key_path)


def test_restore_with_wrong_key_is_rejected(tmp_path, monkeypatch):
    _patch_backup_io(monkeypatch)
    right_key = tmp_path / "right.key"
    wrong_key = tmp_path / "wrong.key"
    generate_key_file(right_key)
    generate_key_file(wrong_key)
    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(
        database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=right_key, encrypt=True, retention=7,
    )
    data_path = backup_dir / meta.file_name

    with pytest.raises(BackupError, match="does not match"):
        rd.restore_backup(
            backup_path=data_path,
            database_url="mysql+pymysql://u:p@127.0.0.1:3306/fake_test",
            target="fake_test", key_file=wrong_key,
        )


# =============================================================================
# restore failure handling
# =============================================================================


def test_restore_reports_mysql_failure_without_leaking_the_password(tmp_path, monkeypatch):
    _patch_backup_io(monkeypatch)
    backup_dir = tmp_path / "backups"
    meta = bd.create_backup(
        database_url="mysql+pymysql://u:SuperSecretPW1@127.0.0.1:3306/fake_test",
        backup_dir=backup_dir, key_file=None, encrypt=False, retention=7,
    )
    data_path = backup_dir / meta.file_name

    monkeypatch.setattr(rd, "ensure_target_exists", lambda url: None)
    monkeypatch.setattr(rd, "ensure_target_is_empty", lambda url: None)
    monkeypatch.setattr(rd, "find_mysql_binary", lambda name: "fake-mysql")

    class _FakeResult:
        returncode = 1
        stderr = b"ERROR 1045 (28000): Access denied for user 'u'@'localhost'"

    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: _FakeResult())

    with pytest.raises(BackupError) as excinfo:
        rd.restore_backup(
            backup_path=data_path,
            database_url="mysql+pymysql://u:SuperSecretPW1@127.0.0.1:3306/fake_test",
            target="fake_test", key_file=None,
        )
    assert "SuperSecretPW1" not in str(excinfo.value)
    assert "Access denied" in str(excinfo.value)


def test_ensure_target_exists_fails_clearly_when_unreachable():
    bad_url = make_url("mysql+pymysql://nobody:nothing@127.0.0.1:1/does_not_matter")
    with pytest.raises(BackupError, match="Cannot connect"):
        rd.ensure_target_exists(bad_url)


def test_verify_restored_data_wraps_mysql_errors_with_context(monkeypatch):
    """A table recorded at backup time that no longer matches anything in
    the restored database (renamed, dropped) must surface as a clear
    BackupError, with the original error preserved as __cause__, and must
    say plainly that the restore itself already ran -- not pretend nothing
    happened."""
    monkeypatch.setattr(pymysql, "connect", lambda **kw: _FakeConnection(raise_for_table="missing_table"))
    url = make_url("mysql+pymysql://u:p@127.0.0.1:3306/fake_test")

    with pytest.raises(BackupError) as excinfo:
        rd.verify_restored_data(url, {"missing_table": 3})

    assert "missing_table" in str(excinfo.value)
    assert "already completed" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, pymysql.err.OperationalError)


# =============================================================================
# the live drill -- real MySQL, disposable *_test database only
# =============================================================================


@pytest.fixture
def mysql_test_url():
    uri = os.environ.get("TEST_DATABASE_URL", "")
    if not uri.startswith("mysql"):
        pytest.skip("backup/restore drill needs TEST_DATABASE_URL=mysql+pymysql://...*_test")
    assert_disposable_test_database(uri)
    return uri


def test_live_backup_and_restore_drill(mysql_test_url, tmp_path):
    """The real article: creates data, backs it up, destroys the data,
    restores from the backup, and verifies the actual rows are back --
    not just that every command returned 0."""
    import pymysql

    url = make_url(mysql_test_url)
    conn = pymysql.connect(host=url.host, port=url.port or 3306, user=url.username,
                            password=url.password or "", database=url.database)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS qj_backup_drill")
            cur.execute("CREATE TABLE qj_backup_drill (id INT PRIMARY KEY, label VARCHAR(50)) ENGINE=InnoDB")
            cur.executemany("INSERT INTO qj_backup_drill (id, label) VALUES (%s, %s)",
                             [(1, "alpha"), (2, "beta"), (3, "gamma")])
        conn.commit()
    finally:
        conn.close()

    key_path = tmp_path / "drill.key"
    generate_key_file(key_path)
    backup_dir = tmp_path / "backups"

    try:
        meta = bd.create_backup(
            database_url=mysql_test_url, backup_dir=backup_dir,
            key_file=key_path, encrypt=True, retention=7,
        )
        assert meta.table_row_counts.get("qj_backup_drill") == 3
        backup_path = backup_dir / meta.file_name

        # Simulate total data loss: restore now refuses any target that has
        # even one table left in it (see ensure_target_is_empty), so a
        # realistic disaster-recovery drill drops everything, not just the
        # one table this test cares about.
        conn = pymysql.connect(host=url.host, port=url.port or 3306, user=url.username,
                                password=url.password or "", database=url.database)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                for (table,) in cur.fetchall():
                    cur.execute(f"DROP TABLE `{table}`")
            conn.commit()
        finally:
            conn.close()

        results = rd.restore_backup(
            backup_path=backup_path, database_url=mysql_test_url,
            target=url.database, key_file=key_path,
        )
        assert results["qj_backup_drill"] == (3, 3)

        conn = pymysql.connect(host=url.host, port=url.port or 3306, user=url.username,
                                password=url.password or "", database=url.database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, label FROM qj_backup_drill ORDER BY id")
                rows = cur.fetchall()
        finally:
            conn.close()
        assert rows == ((1, "alpha"), (2, "beta"), (3, "gamma"))
    finally:
        conn = pymysql.connect(host=url.host, port=url.port or 3306, user=url.username,
                                password=url.password or "", database=url.database)
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS qj_backup_drill")
            conn.commit()
        finally:
            conn.close()
