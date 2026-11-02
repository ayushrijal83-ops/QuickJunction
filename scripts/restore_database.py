"""Quick Junction -- MySQL restore (M7.2).

    python scripts/restore_database.py --backup <path> --target quick_junction_test
    python scripts/restore_database.py --backup <path> --target quick_junction \\
        --allow-production-target --confirm "RESTORE quick_junction"

Restores a backup made by scripts/backup_database.py into an explicitly
named, already-existing target database. Verifies the backup's SHA-256
checksum against its metadata sidecar before touching anything, decrypts it
if needed, runs it through `mysql`, then checks the restored database's
tables and row counts against what backup time recorded.

Safety (non-negotiable, not configurable away by an ordinary flag):
  - The target may never be a MySQL system schema (mysql, sys,
    information_schema, performance_schema).
  - By default the target database name must end in "_test" -- this is the
    only mode an automated test or an unattended drill may use.
  - Restoring into anything else requires BOTH --allow-production-target
    AND --confirm "RESTORE <target>" (the exact target name, typed out) --
    the same pattern scripts/reset_database.bat already uses for its own
    destructive action. There is no way to skip this with a short flag or
    an environment variable; it has to be typed for this specific target,
    every time.
  - The target must be completely empty (no tables at all) -- checked with
    SHOW TABLES right before restoring, unconditionally, for every target
    including a `_test` one. This does not depend on the target's name or
    on --allow-production-target/--confirm; see ensure_target_is_empty()
    below for why (mysqldump's own output cannot be trusted to protect a
    same-named existing table) and for the one known limitation (a
    time-of-check/time-of-use race if something else writes to the target
    between this check and the restore).

The target database must already exist and be reachable with the same
DATABASE_URL credentials pattern (only the database name changes) -- this
tool does not create databases or grant privileges; see
docs/DATABASE_SETUP_GUIDE.md.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts._backup_common import (  # noqa: E402
    BackupError,
    BackupMetadata,
    find_mysql_binary,
    is_disposable_test_name,
    mysql_defaults_file,
    ok,
    say,
    sha256_file,
    step,
    validate_database_name,
    warn,
)
from scripts._backup_crypto import decrypt_bytes, key_fingerprint, load_key  # noqa: E402

load_dotenv(ROOT / ".env")


def load_metadata(backup_path: Path) -> BackupMetadata:
    # backup_path is e.g. quick_junction_test_20261101T....sql or .sql.enc;
    # the sidecar always has the same stem with .json, regardless of how
    # many suffixes the data file has.
    stem = backup_path.name
    for suffix in (".sql.enc", ".sql"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    meta_path = backup_path.parent / f"{stem}.json"
    if not meta_path.is_file():
        raise BackupError(f"No metadata sidecar found for {backup_path.name} (expected {meta_path.name}).")
    return BackupMetadata.from_json(meta_path.read_text(encoding="utf-8"))


def verify_target_allowed(target: str, allow_production_target: bool, confirm: str | None) -> None:
    validate_database_name(target)
    if is_disposable_test_name(target):
        return
    if not allow_production_target:
        raise BackupError(
            f"{target!r} does not end in '_test'. Restoring into it requires "
            f"--allow-production-target and --confirm \"RESTORE {target}\"."
        )
    expected = f"RESTORE {target}"
    if confirm != expected:
        raise BackupError(
            f"--confirm did not match exactly. Expected {expected!r}, got {confirm!r}. "
            "Nothing was restored."
        )


def target_connection_info(database_url: str, target: str):
    url = make_url(database_url)
    return url.set(database=target)


def ensure_target_exists(url) -> None:
    import pymysql

    try:
        conn = pymysql.connect(
            host=url.host, port=url.port or 3306, user=url.username,
            password=url.password or "", database=url.database,
        )
        conn.close()
    except Exception as exc:
        raise BackupError(
            f"Cannot connect to target database {url.database!r}: {exc}. "
            "The target must already exist (this tool never creates one) -- "
            "see docs/DATABASE_SETUP_GUIDE.md section 19."
        ) from exc


def ensure_target_is_empty(url) -> None:
    """Refuses to restore into a target that already has any tables.

    mysqldump's own output cannot be trusted to protect an existing table
    with the same name (older backups were made with --add-drop-table on by
    default; see docs/DATABASE_SETUP_GUIDE.md section 19), so this check is
    unconditional: it does not look at the target's name and is not
    skippable with --allow-production-target, --confirm, or any other flag.
    It applies identically to a `_test`-suffixed target and a production
    one.

    Known limitation (time-of-check/time-of-use): nothing stops another
    connection from creating a table in the target between this check and
    the mysql restore that follows a few lines later. This tool does not
    hold a lock across that window -- a target that is handed only to this
    restore run, as documented, is not exposed to that race in practice."""
    import pymysql

    conn = pymysql.connect(
        host=url.host, port=url.port or 3306, user=url.username,
        password=url.password or "", database=url.database,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            existing = sorted(row[0] for row in cur.fetchall())
    finally:
        conn.close()
    if existing:
        shown = ", ".join(existing[:5]) + (", ..." if len(existing) > 5 else "")
        raise BackupError(
            f"Target database {url.database!r} already has {len(existing)} table(s) "
            f"({shown}). Restore targets must be completely empty -- this tool refuses to "
            "restore into a database that already contains tables, regardless of "
            "--allow-production-target or --confirm, because a restore can drop and replace "
            "any table whose name matches one in the backup. Create a fresh empty database "
            "for this restore (see docs/DATABASE_SETUP_GUIDE.md section 19), or drop its "
            "tables yourself first if you are certain that is what you want."
        )


def run_mysql_restore(mysql_path: str, url, sql_path: Path) -> None:
    with mysql_defaults_file(url.host, url.port or 3306, url.username, url.password or "") as defaults_file:
        cmd = [mysql_path, f"--defaults-extra-file={defaults_file}", "--default-character-set=utf8mb4", url.database]
        try:
            with open(sql_path, "rb") as fh:
                result = subprocess.run(cmd, stdin=fh, stderr=subprocess.PIPE, timeout=3600)
        except subprocess.TimeoutExpired as exc:
            raise BackupError(
                "mysql restore did not finish within 3600 seconds and was aborted. The target "
                "database may now be partially restored -- mysql executes statements "
                "sequentially and a killed process does not roll back ones already applied. "
                "Inspect the target by hand before retrying or treating it as usable."
            ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise BackupError(f"mysql restore exited {result.returncode}: {stderr.strip()[:500]}")


def verify_restored_data(url, expected_counts: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Returns {table: (expected, actual)} for every table recorded at
    backup time. An empty dict from the backup (row counts unavailable at
    backup time) means nothing to compare -- not a pass, just nothing
    checked, and this is reported as such, not silently treated as success."""
    import pymysql.err  # also binds `pymysql` itself, used for .connect() below

    conn = pymysql.connect(
        host=url.host, port=url.port or 3306, user=url.username,
        password=url.password or "", database=url.database,
    )
    try:
        results: dict[str, tuple[int, int]] = {}
        with conn.cursor() as cur:
            for table, expected in expected_counts.items():
                # table names come from the backup's own metadata (written by
                # SHOW TABLES at backup time), never from external input
                try:
                    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                    actual = cur.fetchone()[0]
                except pymysql.err.Error as exc:
                    raise BackupError(
                        f"Could not verify table {table!r} after restore: {exc}. "
                        "The mysql restore itself already completed before this check ran -- "
                        "the target database was modified even though this verification step "
                        "failed. This usually means the table was renamed or dropped between "
                        "backup time and restore time; inspect the target database by hand."
                    ) from exc
                results[table] = (expected, actual)
        return results
    finally:
        conn.close()


def restore_backup(
    backup_path: Path,
    database_url: str,
    target: str,
    key_file: Path | None,
    allow_production_target: bool = False,
    confirm: str | None = None,
) -> dict[str, tuple[int, int]]:
    verify_target_allowed(target, allow_production_target, confirm)

    if not backup_path.is_file():
        raise BackupError(f"Backup file not found: {backup_path}")

    step("Verifying backup integrity before touching the target database")
    meta = load_metadata(backup_path)
    actual_checksum = sha256_file(backup_path)
    if actual_checksum != meta.sha256:
        raise BackupError(
            f"Checksum mismatch for {backup_path.name}: recorded {meta.sha256[:16]}..., "
            f"actual {actual_checksum[:16]}.... This backup is corrupted or was tampered "
            "with; refusing to restore it."
        )
    ok(f"Checksum verified: {meta.sha256[:16]}...")

    key = None
    if meta.encrypted:
        step("Verifying the encryption key before attempting anything else")
        if key_file is None:
            raise BackupError("This backup is encrypted; --key-file is required.")
        key = load_key(key_file)
        if meta.key_fingerprint and key_fingerprint(key) != meta.key_fingerprint:
            raise BackupError("The given key does not match the key this backup was encrypted with.")
        ok("Key matches this backup's fingerprint")

    target_url = target_connection_info(database_url, target)
    ensure_target_exists(target_url)
    ok(f"Target database {target!r} is reachable")

    step("Verifying the target database is empty")
    ensure_target_is_empty(target_url)
    ok("Target has no existing tables")

    mysql_path = find_mysql_binary("mysql.exe" if os.name == "nt" else "mysql")

    with tempfile.TemporaryDirectory(prefix="qj_restore_work_") as workdir:
        sql_path = Path(workdir) / "restore.sql"
        if meta.encrypted:
            step("Decrypting backup")
            encrypted_bytes = backup_path.read_bytes()
            plain = decrypt_bytes(encrypted_bytes, key)
            sql_path.write_bytes(plain)
        else:
            sql_path.write_bytes(backup_path.read_bytes())

        step(f"Restoring into {target!r}")
        run_mysql_restore(mysql_path, target_url, sql_path)
        ok("mysql restore completed")
        # sql_path and any decrypted plaintext are removed when the
        # TemporaryDirectory context exits -- never left on disk.

    step("Verifying restored data against the backup's recorded row counts")
    results = verify_restored_data(target_url, meta.table_row_counts)
    mismatches = {t: (e, a) for t, (e, a) in results.items() if e != a}
    for table, (expected, actual) in sorted(results.items()):
        (ok if expected == actual else warn)(f"{table}: expected {expected}, got {actual}")
    if mismatches:
        raise BackupError(f"Row-count mismatch after restore in {len(mismatches)} table(s): {sorted(mismatches)}")
    if not results:
        warn("No row counts were recorded at backup time; nothing was compared.")

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--key-file", default=os.environ.get("QJ_BACKUP_ENCRYPTION_KEY_FILE"))
    parser.add_argument("--allow-production-target", action="store_true")
    parser.add_argument("--confirm", default=None, metavar='"RESTORE <target>"')
    args = parser.parse_args(argv)

    if not args.database_url:
        say("No DATABASE_URL and no --database-url given.")
        return 2

    try:
        restore_backup(
            backup_path=args.backup,
            database_url=args.database_url,
            target=args.target,
            key_file=Path(args.key_file) if args.key_file else None,
            allow_production_target=args.allow_production_target,
            confirm=args.confirm,
        )
    except BackupError as exc:
        say(f"[FAILED] {exc}")
        return 1

    step("Restore complete and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
