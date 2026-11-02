"""Quick Junction -- shared helpers for backup_database.py and restore_database.py.

Not a public module: imported by the two CLI scripts and by
tests/test_backup_restore.py. Mirrors the conventions already established in
scripts/db_setup.py (name validation, console helpers, credentials never on
a command line or in a log line).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

ROOT = Path(__file__).resolve().parent.parent
IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SYSTEM_SCHEMAS = {"mysql", "sys", "information_schema", "performance_schema"}

# mysqldump writes this as its very last line on a clean, complete dump and
# nowhere else. Its presence is the cheapest reliable signal that the dump
# was not truncated by a crash, a killed process or a full disk -- a partial
# dump simply never reaches this line.
DUMP_COMPLETE_MARKER = "-- Dump completed on"
DUMP_HEADER_MARKER = "-- MySQL dump"

# Default backup directory lives OUTSIDE the repository by construction, so
# "never store backups inside the Git repository" holds even if .gitignore
# is ever edited incorrectly. QJ_BACKUP_DIR overrides it.
DEFAULT_BACKUP_DIR = ROOT.parent / "quickjunction_backups"


class BackupError(RuntimeError):
    """Raised for any backup/restore failure. Message text is safe to print
    and safe to log -- never built from a password, key or raw DSN."""


@dataclass
class BackupMetadata:
    """Everything recorded about one backup. Never includes a password or
    the encryption key itself -- only enough to identify which key is
    needed (see key_fingerprint)."""

    tool_version: str
    created_at: str  # UTC ISO 8601
    database: str  # db name only, never user/password
    host: str
    port: int
    file_name: str
    file_size_bytes: int
    sha256: str
    encrypted: bool
    key_fingerprint: str | None  # sha256 of the key, never the key itself
    mysqldump_version: str | None
    table_row_counts: dict[str, int]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "BackupMetadata":
        return cls(**json.loads(text))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say("")
    say("==> " + msg)


def ok(msg: str) -> None:
    say("    [OK] " + msg)


def warn(msg: str) -> None:
    say("    [WARN] " + msg)


def validate_database_name(name: str) -> None:
    """Raises BackupError for anything that is not a plain MySQL identifier
    or that names a system schema. Used by both scripts before the name
    reaches a shell command or SQL."""
    if not name or not IDENT.match(name):
        raise BackupError(f"{name!r} is not a valid database name (letters, digits, underscore only).")
    if name.lower() in SYSTEM_SCHEMAS:
        raise BackupError(f"{name!r} is a MySQL system schema and may not be used as a backup/restore target.")


def is_disposable_test_name(name: str) -> bool:
    return name.endswith("_test")


# --- MySQL client discovery -------------------------------------------------------------
def find_mysql_binary(name: str) -> str:
    """Locate mysql.exe / mysqldump.exe the same way scripts/find_mysql.bat
    does: an explicit override env var, then PATH, then the standard
    installer folder. Raises BackupError if nothing is found -- never
    guesses silently."""
    override = os.environ.get(f"QJ_{name.upper().replace('.EXE', '')}_PATH")
    if override:
        if not Path(override).is_file():
            raise BackupError(f"{override!r} (from QJ_{name.upper()}_PATH) does not exist.")
        return override

    on_path = shutil.which(name)
    if on_path:
        return on_path

    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    mysql_root = program_files / "MySQL"
    if mysql_root.is_dir():
        for candidate in sorted(mysql_root.glob("MySQL Server *")):
            exe = candidate / "bin" / name
            if exe.is_file():
                return str(exe)

    raise BackupError(
        f"{name} was not found (checked PATH and the standard MySQL install folder). "
        f"Set QJ_{name.upper().replace('.EXE', '')}_PATH to its full path, "
        f"or see docs/DATABASE_SETUP_GUIDE.md section 4."
    )


# --- credentials: never on a command line, never in a log --------------------------------
_UNSAFE_OPTION_FILE_CHARS = {"\n", "\r", "\0"}


def quote_mysql_option_value(value: str) -> str:
    """Quote a value for a MySQL option ("`.cnf`") file so that '#', spaces
    and quote characters in it cannot be misread as a comment or end the
    value early -- per the MySQL manual's "Option Files" escaping rules,
    backslash and double-quote are escaped inside a double-quoted value.

    Raises BackupError for a control character (real newline, carriage
    return or NUL): those would corrupt the file's line-based format no
    matter how they are quoted, so they are rejected outright rather than
    risk an unsafe or client-version-dependent escape."""
    if any(ch in value for ch in _UNSAFE_OPTION_FILE_CHARS):
        raise BackupError(
            "This password contains a newline or NUL character, which cannot be written "
            "safely into a MySQL option file. Use a password without control characters."
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@contextlib.contextmanager
def mysql_defaults_file(host: str, port: int, user: str, password: str):
    """A temporary MySQL "option file" so the password never appears as a
    command-line argument (visible in the process list and in any shell
    history) and never passes through an environment variable a child
    process might log. Deleted unconditionally on exit."""
    quoted_password = quote_mysql_option_value(password)
    fd, path = tempfile.mkstemp(prefix="qj_backup_", suffix=".cnf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("[client]\n")
            fh.write(f"host={host}\n")
            fh.write(f"port={port}\n")
            fh.write(f"user={user}\n")
            fh.write(f"password={quoted_password}\n")
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --- checksums and file integrity --------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_bytes(final_path: Path, data: bytes) -> None:
    """Write-then-rename so a reader never sees a partially written file
    under the final name. os.replace is atomic on the same filesystem on
    both Windows and POSIX."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(final_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, final_path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
