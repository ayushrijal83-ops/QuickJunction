"""M7.3 -- database privilege separation.

Unit tests (no MySQL server needed) cover scripts/db_setup.py's account
creation/grant logic, the runtime vs. migration URL plumbing, and -- without
touching a real database at all -- prove that migrations/env.py's
MIGRATION_DATABASE_URL override actually changes which server Alembic tries
to reach (by pointing it at a closed port and watching the connection
attempt fail against *that* port, not against the working SQLite database
DATABASE_URL points at).

test_live_privilege_separation_drill is the real article: it runs only when
TEST_DATABASE_URL points at a disposable `*_test` MySQL database (the same
convention tests/test_mysql_concurrency.py and
tests/test_backup_restore.py::test_live_backup_and_restore_drill use) *and*
QJ_MYSQL_ADMIN_PASSWORD names an account on that same server able to CREATE
USER/GRANT/REVOKE -- creating MySQL accounts needs more than the ordinary
disposable-database login. It creates two brand-new, randomly-named
accounts (never the real qj_user/qj_migrator), proves runtime DML works and
runtime DDL is denied, proves the migration account can run the real
migration chain end to end (including the data-backfill migrations), and
drops both accounts and every table in the disposable database afterward.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import pymysql
import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.db_setup as ds  # noqa: E402
from tests.conftest import assert_disposable_test_database  # noqa: E402


# =============================================================================
# fake MySQL cursor -- just enough of pymysql's cursor protocol for
# ensure_app_user()/ensure_migrator_user()/user_exists() to run against
# =============================================================================


class _FakeDbCursor:
    def __init__(self, existing=()):
        self.existing = set(existing)  # {(user, host), ...}
        self.calls: list[tuple[str, tuple | None]] = []
        self._last_sql = ""
        self._last_params = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last_sql, self._last_params = sql, params

    def fetchone(self):
        if "FROM mysql.user" in self._last_sql:
            return (1,) if tuple(self._last_params) in self.existing else (0,)
        return (0,)

    def grants(self):
        return [sql for sql, _ in self.calls if sql.startswith("GRANT")]

    def creates(self):
        return [sql for sql, _ in self.calls if sql.startswith("CREATE USER")]

    def alters(self):
        return [sql for sql, _ in self.calls if sql.startswith("ALTER USER")]


BASE_SETTINGS = {"host": "127.0.0.1", "port": 3306, "db": "quick_junction",
                  "user": "qj_user", "migrator_user": "qj_migrator"}


# =============================================================================
# settings() -- account name validation
# =============================================================================


def test_settings_defaults_to_qj_migrator(monkeypatch):
    for var in ("QJ_DB_HOST", "QJ_DB_PORT", "QJ_DB_NAME", "QJ_DB_USER", "QJ_DB_MIGRATOR_USER"):
        monkeypatch.delenv(var, raising=False)
    s = ds.settings()
    assert s["user"] == "qj_user"
    assert s["migrator_user"] == "qj_migrator"


def test_settings_rejects_root_as_migrator(monkeypatch):
    monkeypatch.setenv("QJ_DB_MIGRATOR_USER", "root")
    with pytest.raises(SystemExit):
        ds.settings()


def test_settings_rejects_same_name_for_both_accounts(monkeypatch):
    monkeypatch.setenv("QJ_DB_MIGRATOR_USER", "qj_user")
    with pytest.raises(SystemExit):
        ds.settings()


def test_settings_rejects_invalid_migrator_identifier(monkeypatch):
    monkeypatch.setenv("QJ_DB_MIGRATOR_USER", "not valid; DROP TABLE x")
    with pytest.raises(SystemExit):
        ds.settings()


# =============================================================================
# app_url() -- runtime vs. migrator URL construction
# =============================================================================


def test_app_url_defaults_to_the_runtime_user():
    url = ds.app_url(BASE_SETTINGS, "p@ss")
    assert url == "mysql+pymysql://qj_user:p%40ss@127.0.0.1:3306/quick_junction"


def test_app_url_accepts_an_explicit_user_override_for_the_migrator():
    url = ds.app_url(BASE_SETTINGS, "secret", user="qj_migrator")
    assert url.startswith("mysql+pymysql://qj_migrator:")
    assert "@127.0.0.1:3306/quick_junction" in url


# =============================================================================
# child_env() -- MIGRATION_DATABASE_URL only present when explicitly given
# =============================================================================


def test_child_env_includes_migration_url_when_given():
    env = ds.child_env("mysql+pymysql://qj_user:x@h/db", "mysql+pymysql://qj_migrator:y@h/db")
    assert env["DATABASE_URL"] == "mysql+pymysql://qj_user:x@h/db"
    assert env["MIGRATION_DATABASE_URL"] == "mysql+pymysql://qj_migrator:y@h/db"


def test_child_env_clears_a_stale_migration_url_when_not_given(monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "mysql+pymysql://leftover:z@h/db")
    env = ds.child_env("mysql+pymysql://qj_user:x@h/db")
    assert "MIGRATION_DATABASE_URL" not in env


# =============================================================================
# ensure_app_user() / ensure_migrator_user() -- exact grants, CREATE vs ALTER
# =============================================================================


def test_ensure_app_user_grants_dml_only_for_a_new_account():
    cur = _FakeDbCursor()
    ds.ensure_app_user(cur, BASE_SETTINGS, "pw", password_from_env=False)
    assert len(cur.creates()) == 2  # 127.0.0.1 and localhost
    assert cur.alters() == []
    grants = cur.grants()
    assert len(grants) == 2
    assert all(ds.RUNTIME_PRIVILEGES in g for g in grants)
    assert all("CREATE" not in g and "DROP" not in g for g in grants)


def test_ensure_app_user_alters_an_existing_account(monkeypatch):
    monkeypatch.setattr(ds, "confirm", lambda *a, **k: True)
    cur = _FakeDbCursor(existing={("qj_user", "127.0.0.1"), ("qj_user", "localhost")})
    ds.ensure_app_user(cur, BASE_SETTINGS, "pw", password_from_env=False)
    assert len(cur.alters()) == 2
    assert cur.creates() == []


def test_ensure_migrator_user_grants_dml_and_ddl_for_a_new_account():
    cur = _FakeDbCursor()
    ds.ensure_migrator_user(cur, BASE_SETTINGS, "pw")
    assert len(cur.creates()) == 2
    grants = cur.grants()
    assert len(grants) == 2
    assert all(ds.MIGRATOR_PRIVILEGES in g for g in grants)
    assert all(p in g for g in grants for p in ("CREATE", "ALTER", "DROP", "INDEX", "REFERENCES"))


def test_ensure_migrator_user_never_prompts_and_always_resets_password():
    """The migrator's password is never persisted anywhere, so there is
    nothing to protect by asking first -- unlike ensure_app_user(), this
    must not call confirm() even when the account already exists."""
    cur = _FakeDbCursor(existing={("qj_migrator", "127.0.0.1"), ("qj_migrator", "localhost")})
    ds.ensure_migrator_user(cur, BASE_SETTINGS, "fresh-password")
    assert len(cur.alters()) == 2
    assert cur.creates() == []


# =============================================================================
# cmd_narrow_runtime() -- the explicit, opt-in upgrade path never auto-runs
# =============================================================================


def test_narrow_runtime_requires_the_exact_phrase_and_never_connects(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "not the phrase")
    called = []
    monkeypatch.setattr(ds, "admin_connect", lambda s: called.append(s) or (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(SystemExit):
        ds.cmd_narrow_runtime()
    assert called == [], "a non-matching phrase must never reach admin_connect()"


# =============================================================================
# migrations/env.py -- MIGRATION_DATABASE_URL actually overrides the
# connection Alembic uses, proven without any MySQL server at all
# =============================================================================


def test_flask_db_upgrade_uses_migration_database_url_when_set(tmp_path):
    """DATABASE_URL points at a SQLite file the app can boot against;
    MIGRATION_DATABASE_URL points at a closed TCP port. If the override in
    migrations/env.py works, Alembic tries to reach that port (and fails
    fast, since nothing listens there) instead of successfully migrating
    the SQLite file -- proving the override, not just its absence of an
    error, took effect."""
    db_path = (tmp_path / "x.db").as_posix()
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env["MIGRATION_DATABASE_URL"] = "mysql+pymysql://nobody:nothing@127.0.0.1:1/does_not_matter"
    env["APP_ENV"] = "development"
    env["SECRET_KEY"] = "x" * 64
    env["LLM_WARMUP"] = "false"
    env.pop("QJ_MYSQL_ADMIN_PASSWORD", None)

    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode != 0, "upgrade must fail: nothing listens on 127.0.0.1:1"
    combined = result.stderr + result.stdout
    assert "127.0.0.1" in combined, f"expected a connection attempt to 127.0.0.1:1, got:\n{combined}"
    assert not Path(db_path).exists() or Path(db_path).stat().st_size == 0, (
        "no table should exist in the SQLite file -- the migrator URL must have been used instead of it"
    )


def test_flask_db_upgrade_falls_back_to_database_url_when_migration_url_absent(tmp_path):
    """Backward compatibility: an installation that has not set
    MIGRATION_DATABASE_URL at all must migrate exactly as it did before
    M7.3 -- straight against DATABASE_URL."""
    db_path = (tmp_path / "y.db").as_posix()
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env.pop("MIGRATION_DATABASE_URL", None)
    env["APP_ENV"] = "development"
    env["SECRET_KEY"] = "x" * 64
    env["LLM_WARMUP"] = "false"
    env.pop("QJ_MYSQL_ADMIN_PASSWORD", None)

    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert Path(db_path).stat().st_size > 0, "the fallback path must have actually migrated the SQLite file"


# =============================================================================
# the live drill -- real MySQL, disposable *_test database, throwaway accounts
# =============================================================================


@pytest.fixture
def privsep_live(tmp_path):
    uri = os.environ.get("TEST_DATABASE_URL", "")
    if not uri.startswith("mysql"):
        pytest.skip("privilege-separation drill needs TEST_DATABASE_URL=mysql+pymysql://...*_test")
    assert_disposable_test_database(uri)
    admin_user = os.environ.get("QJ_MYSQL_ADMIN_USER", "root")
    admin_password = os.environ.get("QJ_MYSQL_ADMIN_PASSWORD")
    if admin_password is None:
        pytest.skip("privilege-separation drill also needs QJ_MYSQL_ADMIN_PASSWORD -- an account able to "
                     "CREATE USER/GRANT/REVOKE on the TEST_DATABASE_URL server (creating MySQL accounts "
                     "needs more than the disposable database's own login)")
    url = make_url(uri)
    try:
        conn = pymysql.connect(host=url.host, port=url.port or 3306, user=admin_user,
                               password=admin_password, autocommit=True, connect_timeout=10)
        conn.close()
    except Exception as exc:
        pytest.skip(f"privilege-separation drill: admin login failed ({exc.__class__.__name__}: {exc})")
    return url, admin_user, admin_password


def test_live_privilege_separation_drill(privsep_live):
    """Creates two brand-new, randomly-named accounts on the disposable
    *_test database (never the real qj_user/qj_migrator), and proves:
    1. the migration account can run the real migration chain, including
       the data-backfill migrations (e.g. 7c4e1a9b52d3's staff grandfather
       UPDATE), end to end;
    2. the runtime account can do ordinary SELECT/INSERT;
    3. the runtime account is genuinely denied DDL by MySQL itself, not
       just by convention.
    Drops both accounts and every table in the disposable database
    afterward, regardless of outcome."""
    url, admin_user, admin_password = privsep_live
    db, host, port = url.database, url.host, url.port or 3306

    suffix = uuid4().hex[:10]
    runtime_user = f"qjps_rt_{suffix}"
    migrator_user = f"qjps_mg_{suffix}"
    runtime_password = secrets.token_urlsafe(24)
    migrator_password = secrets.token_urlsafe(24)
    hosts = ("127.0.0.1", "localhost")

    admin = pymysql.connect(host=host, port=port, user=admin_user, password=admin_password, autocommit=True)
    try:
        with admin.cursor() as cur:
            # Clean slate: this database is *_test by assert_disposable_test_database,
            # never the real application's database.
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (db,))
            for (table,) in cur.fetchall():
                cur.execute(f"DROP TABLE `{table}`")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")

            for h in hosts:
                cur.execute(f"CREATE USER '{runtime_user}'@'{h}' IDENTIFIED BY %s", (runtime_password,))
                cur.execute(f"GRANT {ds.RUNTIME_PRIVILEGES} ON `{db}`.* TO '{runtime_user}'@'{h}'")
                cur.execute(f"CREATE USER '{migrator_user}'@'{h}' IDENTIFIED BY %s", (migrator_password,))
                cur.execute(f"GRANT {ds.MIGRATOR_PRIVILEGES} ON `{db}`.* TO '{migrator_user}'@'{h}'")

        runtime_url = f"mysql+pymysql://{runtime_user}:{quote_plus(runtime_password)}@{host}:{port}/{db}"
        migrator_url = f"mysql+pymysql://{migrator_user}:{quote_plus(migrator_password)}@{host}:{port}/{db}"

        step_env = dict(os.environ)
        step_env["DATABASE_URL"] = runtime_url
        step_env["MIGRATION_DATABASE_URL"] = migrator_url
        step_env["LLM_WARMUP"] = "false"
        step_env.pop("QJ_MYSQL_ADMIN_PASSWORD", None)

        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade"],
            cwd=str(ROOT), env=step_env, capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, (
            f"migration account could not run the real migration chain:\n{result.stderr or result.stdout}"
        )

        conn = pymysql.connect(host=host, port=port, user=runtime_user, password=runtime_password, database=db)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version_num FROM alembic_version")
                assert cur.fetchone(), "migrations did not actually apply"
                # the staff-grandfather migration's effect: a runtime SELECT must work
                cur.execute("SELECT COUNT(*) FROM users")
                cur.fetchone()
                new_username = f"privsep_{suffix}"
                cur.execute(
                    "INSERT INTO users (username, email, password_hash, role, is_active, session_version,"
                    " created_at, updated_at) VALUES (%s, %s, 'x', 'customer', 1, 0, NOW(), NOW())",
                    (new_username, f"{new_username}@example.com"),
                )
                conn.commit()
                cur.execute("SELECT COUNT(*) FROM users WHERE username = %s", (new_username,))
                assert cur.fetchone()[0] == 1, "runtime INSERT did not take effect"
        finally:
            conn.close()

        conn = pymysql.connect(host=host, port=port, user=runtime_user, password=runtime_password, database=db)
        try:
            with pytest.raises(pymysql.err.OperationalError, match=r"(?i)command denied"):
                with conn.cursor() as cur:
                    cur.execute("ALTER TABLE users ADD COLUMN privsep_probe INT")
        finally:
            conn.close()
    finally:
        with admin.cursor() as cur:
            for user in (runtime_user, migrator_user):
                for h in hosts:
                    cur.execute(f"DROP USER IF EXISTS '{user}'@'{h}'")
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (db,))
            for (table,) in cur.fetchall():
                cur.execute(f"DROP TABLE `{table}`")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        admin.close()
