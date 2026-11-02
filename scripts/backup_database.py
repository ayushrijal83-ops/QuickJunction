"""Quick Junction -- MySQL backup (M7.2).

    python scripts/backup_database.py
    python scripts/backup_database.py --database-url mysql+pymysql://qj_user:...@127.0.0.1:3306/quick_junction_test
    python scripts/backup_database.py --generate-key C:\\secure\\qj_backup.key
    python scripts/backup_database.py --no-encryption   # local/dev convenience only

What a backup is: a `mysqldump --single-transaction --routines
--default-character-set=utf8mb4` of the database named in DATABASE_URL (or
--database-url), the same flags already documented by hand in
docs/DATABASE_SETUP_GUIDE.md section 19 -- a full structural + data dump,
consistent for InnoDB without locking the live application. Written
atomically (temp file, verified, then renamed into place), checksummed,
optionally encrypted at rest with a Fernet key (see scripts/_backup_crypto.py),
and recorded in a sidecar `.json` metadata file next to it. A failed backup
never leaves a file under its final name -- there is nothing to mistake for
a completed one.

Configuration (all optional; environment variables, read once at startup):
    DATABASE_URL                     what to back up (required unless --database-url)
    QJ_BACKUP_DIR                    where backups are written
                                      (default: a folder OUTSIDE the repository,
                                      ../quickjunction_backups relative to this
                                      project -- backups are never stored inside
                                      the Git repository)
    QJ_BACKUP_ENCRYPTION_KEY_FILE    path to a Fernet key file (see --generate-key)
    QJ_BACKUP_RETENTION_COUNT        how many backups of this database to keep
                                      (default 7; oldest beyond this are deleted
                                      after a successful backup -- the newest
                                      backup is never deleted, even if this is 0)
    QJ_MYSQLDUMP_PATH                override: full path to mysqldump.exe

No password is ever written to a command line, an environment variable
passed to a child process, or a log line -- see mysql_defaults_file() in
_backup_common.py.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts._backup_common import (  # noqa: E402
    BackupError,
    BackupMetadata,
    DEFAULT_BACKUP_DIR,
    DUMP_COMPLETE_MARKER,
    DUMP_HEADER_MARKER,
    __version__,
    atomic_write_bytes,
    find_mysql_binary,
    mysql_defaults_file,
    now_utc_iso,
    ok,
    say,
    sha256_file,
    step,
    validate_database_name,
    warn,
)
from scripts._backup_crypto import encrypt_bytes, key_fingerprint, load_key  # noqa: E402

load_dotenv(ROOT / ".env")


def table_row_counts(url) -> dict[str, int]:
    """Read-only. Used only for the metadata sidecar (a restore-verification
    aid), never for the dump itself -- mysqldump reads the data directly."""
    import pymysql

    conn = pymysql.connect(
        host=url.host, port=url.port or 3306, user=url.username,
        password=url.password or "", database=url.database,
    )
    try:
        counts: dict[str, int] = {}
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [row[0] for row in cur.fetchall()]
            for table in tables:
                # table names come from SHOW TABLES (the server), never from
                # external input, so this is not building SQL from user data
                cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                counts[table] = cur.fetchone()[0]
        return counts
    finally:
        conn.close()


def mysqldump_version(mysqldump_path: str) -> str | None:
    try:
        result = subprocess.run([mysqldump_path, "--version"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run_mysqldump(mysqldump_path: str, url, out_path: Path) -> None:
    with mysql_defaults_file(url.host, url.port or 3306, url.username, url.password or "") as defaults_file:
        cmd = [
            mysqldump_path,
            f"--defaults-extra-file={defaults_file}",
            "--single-transaction",
            "--routines",
            "--default-character-set=utf8mb4",
            # Restore-time safety relies on an explicit empty-target check
            # (see restore_database.py:ensure_target_is_empty), not on the
            # dump's own content -- but a dump with no DROP TABLE statement
            # is a second, independent layer: restoring it into a database
            # that already has a same-named table fails with a plain MySQL
            # "table already exists" error instead of silently replacing it.
            "--skip-add-drop-table",
            url.database,
        ]
        try:
            with open(out_path, "wb") as out:
                result = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, timeout=3600)
        except subprocess.TimeoutExpired as exc:
            raise BackupError(
                "mysqldump did not finish within 3600 seconds and was aborted "
                "(a stalled connection, a locked table, or an unusually large database). "
                "No backup file was written under its final name."
            ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise BackupError(f"mysqldump exited {result.returncode}: {stderr.strip()[:500]}")


def verify_dump_complete(path: Path) -> None:
    """Catches a truncated dump (killed process, disk full mid-write) that
    a zero exit code alone would not -- mysqldump's own completion marker
    is only ever written as the very last line of a clean dump."""
    if not path.is_file() or path.stat().st_size == 0:
        raise BackupError("mysqldump produced an empty file.")
    with open(path, "rb") as fh:
        head = fh.read(4096).decode("utf-8", errors="replace")
        fh.seek(max(0, path.stat().st_size - 4096))
        tail = fh.read(4096).decode("utf-8", errors="replace")
    if DUMP_HEADER_MARKER not in head:
        raise BackupError("Dump file does not start with a MySQL dump header; refusing to treat it as valid.")
    if DUMP_COMPLETE_MARKER not in tail:
        raise BackupError(
            "Dump file has no completion marker at the end -- it looks truncated "
            "(process killed, disk full, or connection lost mid-dump)."
        )


def existing_backups(backup_dir: Path, database: str) -> list[Path]:
    if not backup_dir.is_dir():
        return []
    return sorted(backup_dir.glob(f"{database}_*.json"))


def apply_retention(backup_dir: Path, database: str, retention: int) -> None:
    metas = existing_backups(backup_dir, database)
    if len(metas) <= max(retention, 1):
        return  # never delete down to zero, even if retention is configured as 0
    # oldest first by filename (timestamp-prefixed, see create_backup)
    to_delete = metas[: len(metas) - max(retention, 1)]
    for meta_path in to_delete:
        try:
            meta = BackupMetadata.from_json(meta_path.read_text(encoding="utf-8"))
            data_path = meta_path.with_name(meta.file_name)
            data_path.unlink(missing_ok=True)
        finally:
            meta_path.unlink(missing_ok=True)
        ok(f"Retention: removed {meta_path.stem}")


def create_backup(
    database_url: str,
    backup_dir: Path,
    key_file: Path | None,
    encrypt: bool,
    retention: int,
) -> BackupMetadata:
    url = make_url(database_url)
    if url.get_backend_name() != "mysql":
        raise BackupError(f"Only MySQL is supported; got backend {url.get_backend_name()!r}.")
    validate_database_name(url.database or "")

    step(f"Backing up database {url.database!r} on {url.host}:{url.port or 3306}")
    mysqldump_path = find_mysql_binary("mysqldump.exe" if os.name == "nt" else "mysqldump")
    ok(f"Using {mysqldump_path}")

    key = None
    fingerprint = None
    if encrypt:
        if key_file is None:
            raise BackupError(
                "Encryption is required by default. Pass --key-file (or set "
                "QJ_BACKUP_ENCRYPTION_KEY_FILE), generate one with --generate-key, "
                "or pass --no-encryption to opt out explicitly."
            )
        key = load_key(key_file)
        fingerprint = key_fingerprint(key)

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_utc_iso().replace(":", "").replace("-", "")
    stem = f"{url.database}_{timestamp}"
    suffix = ".sql.enc" if encrypt else ".sql"

    import tempfile

    with tempfile.TemporaryDirectory(prefix="qj_backup_work_") as workdir:
        raw_path = Path(workdir) / "dump.sql"
        step("Running mysqldump (--single-transaction, consistent snapshot)")
        run_mysqldump(mysqldump_path, url, raw_path)
        verify_dump_complete(raw_path)
        ok(f"Dump verified complete: {raw_path.stat().st_size:,} bytes")

        data = raw_path.read_bytes()
        if encrypt:
            step("Encrypting backup at rest (Fernet / AES-128-CBC + HMAC-SHA256)")
            data = encrypt_bytes(data, key)

        final_path = backup_dir / f"{stem}{suffix}"
        atomic_write_bytes(final_path, data)
        ok(f"Written: {final_path}")

        checksum = sha256_file(final_path)

        step("Recording table row counts for later restore verification")
        try:
            counts = table_row_counts(url)
        except Exception as exc:  # pragma: no cover - metadata is best-effort
            warn(f"Could not record row counts: {exc}")
            counts = {}

        meta = BackupMetadata(
            tool_version=__version__,
            created_at=now_utc_iso(),
            database=url.database,
            host=url.host,
            port=url.port or 3306,
            file_name=final_path.name,
            file_size_bytes=final_path.stat().st_size,
            sha256=checksum,
            encrypted=encrypt,
            key_fingerprint=fingerprint,
            mysqldump_version=mysqldump_version(mysqldump_path),
            table_row_counts=counts,
        )
        meta_path = backup_dir / f"{stem}.json"
        atomic_write_bytes(meta_path, meta.to_json().encode("utf-8"))
        ok(f"Metadata: {meta_path}")

    step("Applying retention policy")
    apply_retention(backup_dir, url.database, retention)

    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                         help="defaults to DATABASE_URL")
    parser.add_argument("--backup-dir", default=os.environ.get("QJ_BACKUP_DIR", str(DEFAULT_BACKUP_DIR)))
    parser.add_argument("--key-file", default=os.environ.get("QJ_BACKUP_ENCRYPTION_KEY_FILE"))
    parser.add_argument("--no-encryption", action="store_true",
                         help="write an unencrypted backup (local/dev convenience only)")
    parser.add_argument("--retention", type=int,
                         default=int(os.environ.get("QJ_BACKUP_RETENTION_COUNT", "7")))
    parser.add_argument("--generate-key", metavar="PATH",
                         help="create a new Fernet key file at PATH and exit (does not back up anything)")
    args = parser.parse_args(argv)

    if args.generate_key:
        from scripts._backup_crypto import generate_key_file

        path = Path(args.generate_key)
        generate_key_file(path)
        ok(f"New encryption key written to {path}")
        warn("Store this key somewhere other than the backup directory and "
             "other than this repository. Losing it makes every backup "
             "encrypted with it unrecoverable.")
        return 0

    if not args.database_url:
        say("No DATABASE_URL and no --database-url given.")
        return 2

    try:
        meta = create_backup(
            database_url=args.database_url,
            backup_dir=Path(args.backup_dir),
            key_file=Path(args.key_file) if args.key_file else None,
            encrypt=not args.no_encryption,
            retention=args.retention,
        )
    except BackupError as exc:
        say(f"[FAILED] {exc}")
        return 1

    step("Backup complete")
    ok(f"{meta.file_name} ({meta.file_size_bytes:,} bytes, sha256 {meta.sha256[:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
