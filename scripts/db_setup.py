"""Quick Junction -- MySQL database setup, verification and reset (Windows helper).

Normally started by ``scripts/setup_database.bat``, ``scripts/reset_database.bat``
or ``setup_quick_junction.bat`` rather than directly:

    python scripts/db_setup.py setup    # non-destructive (default)
    python scripts/db_setup.py verify   # check an existing installation
    python scripts/db_setup.py reset    # DESTRUCTIVE: drop + recreate the QJ database

What ``setup`` does, in order, stopping at the first failure:

 1. connects to MySQL as an administrator (usually ``root``) -- used only here,
    never written to ``.env``;
 2. ``CREATE DATABASE IF NOT EXISTS`` (utf8mb4) -- an existing database is kept;
 3. creates the application account ``'qj_user'@'127.0.0.1'`` and ``@'localhost'``
    with privileges on this one database only (no ``ALL``, no global grants);
 4. writes ``DATABASE_URL`` (and ``SECRET_KEY`` if missing) into ``.env``;
 5. connects as the application account;
 6. runs the full migration chain (``flask db upgrade``);
 7. checks the database revision equals the repository head;
 8. checks every model table exists, InnoDB/utf8mb4, and the seeded settings row;
 9. starts the application against the database and requests ``/health`` and
    ``/menu`` (a real read through the ORM).

Passwords are read with ``getpass`` (not echoed) and are never printed. The
application password is generated with ``secrets`` and only stored in ``.env``,
which is git-ignored.

Advanced / automated use (all optional; defaults shown):
    QJ_DB_HOST=127.0.0.1  QJ_DB_PORT=3306  QJ_DB_NAME=quick_junction  QJ_DB_USER=qj_user
    QJ_MYSQL_ADMIN_USER=root  QJ_MYSQL_ADMIN_PASSWORD=<not recommended on shared machines>
    QJ_ASSUME_YES=1  (accept the default answer to every question)
    QJ_LOAD_DEMO=1|0 (answer the "load demo data" question)
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SYSTEM_SCHEMAS = {"mysql", "sys", "information_schema", "performance_schema"}
RESET_PHRASE = "RESET QUICK JUNCTION"

# Privileges the application needs on its own database, and nothing else:
# data access for the running app, plus schema changes for `flask db upgrade`
# (and `downgrade`). SELECT ... FOR UPDATE needs SELECT + UPDATE.
APP_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES"


# --- console helpers ------------------------------------------------------------------
def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say("")
    say("==> " + msg)


def ok(msg):
    say("    [OK] " + msg)


def fail(msg, hint=None):
    say("")
    say("    [FAILED] " + msg)
    if hint:
        say("    What to do: " + hint)
    say("    Setup stopped. Nothing after this step was done.")
    sys.exit(1)


def assume_yes():
    return os.environ.get("QJ_ASSUME_YES") == "1"


def ask(question, default=""):
    if assume_yes():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"    {question}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def confirm(question, default_yes=True):
    if assume_yes():
        return default_yes
    hint = "Y/n" if default_yes else "y/N"
    answer = ask(f"{question} ({hint})").lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def ask_password(prompt):
    env = os.environ.get("QJ_MYSQL_ADMIN_PASSWORD")
    if env is not None:
        return env
    return getpass.getpass(f"    {prompt} (typing is hidden): ")


# --- .env handling -----------------------------------------------------------------------
def read_env_lines():
    if ENV_FILE.exists():
        return ENV_FILE.read_text(encoding="utf-8").splitlines()
    if not ENV_EXAMPLE.exists():
        fail(".env.example is missing, so .env cannot be created.", "Restore .env.example from the repository.")
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def env_value(lines, key):
    for line in lines:
        if line.startswith(key + "="):
            return line[len(key) + 1:].strip()
    return None


def set_env_value(lines, key, value):
    out, done = [], False
    for line in lines:
        if line.startswith(key + "=") and not done:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    return out


def write_env(lines):
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- settings --------------------------------------------------------------------------
def settings():
    s = {
        "host": os.environ.get("QJ_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("QJ_DB_PORT", "3306")),
        "db": os.environ.get("QJ_DB_NAME", "quick_junction"),
        "user": os.environ.get("QJ_DB_USER", "qj_user"),
    }
    for key in ("db", "user"):
        if not IDENT.match(s[key]):
            fail(f"'{s[key]}' is not a valid name.", "Use letters, digits and underscores only.")
    if s["db"].lower() in SYSTEM_SCHEMAS:
        fail(f"'{s['db']}' is a MySQL system database and cannot be used.")
    if s["user"].lower() == "root":
        fail("The application account must not be 'root'.", "Keep the default qj_user.")
    return s


def import_pymysql():
    try:
        import pymysql  # noqa: F401
        return pymysql
    except ImportError:
        fail("The PyMySQL driver is not installed in this Python environment.",
             "Run setup_quick_junction.bat (it creates .venv and installs requirements.txt).")


def admin_connect(s):
    pymysql = import_pymysql()
    step("Connecting to MySQL as an administrator (only for creating the database and user)")
    admin_user = os.environ.get("QJ_MYSQL_ADMIN_USER") or ask("MySQL administrator username", "root")
    for attempt in range(3):
        password = ask_password(f"Password for MySQL user '{admin_user}'")
        try:
            conn = pymysql.connect(host=s["host"], port=s["port"], user=admin_user, password=password,
                                   autocommit=True, connect_timeout=10)
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else None
            if code == 1045 and os.environ.get("QJ_MYSQL_ADMIN_PASSWORD") is None and attempt < 2:
                say("    Access denied -- wrong username or password. Try again.")
                continue
            if code in (2003, 2002):
                fail(f"Cannot reach a MySQL server at {s['host']}:{s['port']}.",
                     "Start the MySQL service (services.msc -> MySQL80 -> Start) and check the port. "
                     "See docs/DATABASE_SETUP_GUIDE.md, Troubleshooting B and D.")
            fail(f"MySQL refused the administrator login (error {code}).",
                 "Check the username and password you chose when installing MySQL. "
                 "See docs/DATABASE_SETUP_GUIDE.md, Troubleshooting C.")
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        ok(f"Connected to MySQL {version} at {s['host']}:{s['port']} as '{admin_user}'")
        if not version.startswith("8."):
            say(f"    NOTE: Quick Junction was tested on MySQL 8.0. Version {version} is untested.")
        return conn
    fail("Too many failed login attempts.")


def db_exists(cur, name):
    cur.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (name,))
    return cur.fetchone()[0] == 1


def table_count(cur, name):
    cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (name,))
    return cur.fetchone()[0]


def user_exists(cur, user, host):
    cur.execute("SELECT COUNT(*) FROM mysql.user WHERE User = %s AND Host = %s", (user, host))
    return cur.fetchone()[0] == 1


def ensure_database(cur, s):
    step(f"Creating the database '{s['db']}' if it does not exist")
    if db_exists(cur, s["db"]):
        n = table_count(cur, s["db"])
        ok(f"Database '{s['db']}' already exists ({n} tables) -- keeping it, nothing is deleted")
        return
    # Identifier validated against IDENT above; it cannot contain a backtick.
    cur.execute(f"CREATE DATABASE `{s['db']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    ok(f"Database '{s['db']}' created (utf8mb4)")


def existing_app_password(lines, s):
    """The application password already in .env, if .env points at this user/db."""
    url = env_value(lines, "DATABASE_URL")
    if not url:
        return None
    try:
        from sqlalchemy.engine import make_url
        u = make_url(url)
    except Exception:
        return None
    if (u.username == s["user"] and u.database == s["db"] and u.password
            and u.password not in ("CHANGE_ME", "YOUR_PASSWORD")):
        return u.password
    return None


def ensure_app_user(cur, s, password, password_from_env):
    step(f"Creating the application account '{s['user']}' (only for database '{s['db']}')")
    exists = [h for h in ("127.0.0.1", "localhost") if user_exists(cur, s["user"], h)]
    if exists and not password_from_env:
        say(f"    The MySQL account '{s['user']}' already exists, but .env does not contain its password.")
        if not confirm(f"Give '{s['user']}' a new password and store it in .env?", default_yes=True):
            fail("Cannot continue without the application account's password.",
                 "Put the existing password into DATABASE_URL in .env, then re-run.")
    for host in ("127.0.0.1", "localhost"):
        if host in exists:
            cur.execute(f"ALTER USER '{s['user']}'@'{host}' IDENTIFIED BY %s", (password,))
            ok(f"'{s['user']}'@'{host}' exists -- password set to the value stored in .env")
        else:
            cur.execute(f"CREATE USER '{s['user']}'@'{host}' IDENTIFIED BY %s", (password,))
            ok(f"'{s['user']}'@'{host}' created")
        cur.execute(f"GRANT {APP_PRIVILEGES} ON `{s['db']}`.* TO '{s['user']}'@'{host}'")
    ok("Privileges granted on this database only: " + APP_PRIVILEGES)


def app_url(s, password):
    return f"mysql+pymysql://{s['user']}:{quote_plus(password)}@{s['host']}:{s['port']}/{s['db']}"


def update_env(s, password):
    step("Writing the database settings to .env")
    lines = read_env_lines()
    created = not ENV_FILE.exists()
    new_url = app_url(s, password)
    current = env_value(lines, "DATABASE_URL")
    if current and current != new_url and "CHANGE_ME" not in current and not created:
        try:
            from sqlalchemy.engine import make_url
            cu = make_url(current)
            where = f"user '{cu.username}', database '{cu.database}'"
        except Exception:
            where = "a different database"
        say(f"    Your .env currently points to {where}.")
        if not confirm(f"Replace it with user '{s['user']}', database '{s['db']}'?", default_yes=False):
            fail("Kept your existing DATABASE_URL, so setup cannot continue with the new database.",
                 "Re-run and answer 'y', or edit DATABASE_URL in .env yourself.")
    lines = set_env_value(lines, "DATABASE_URL", new_url)
    secret = env_value(lines, "SECRET_KEY") or ""
    if not secret or secret.startswith("replace-with"):
        lines = set_env_value(lines, "SECRET_KEY", secrets.token_urlsafe(64))
        ok("Generated a new SECRET_KEY")
    if not env_value(lines, "APP_ENV"):
        lines = set_env_value(lines, "APP_ENV", "development")
    write_env(lines)
    ok((".env created from .env.example" if created else ".env updated") +
       " (DATABASE_URL set; the password is not shown)")


# --- migrations and verification ------------------------------------------------------------
def child_env(url):
    env = dict(os.environ)
    env["DATABASE_URL"] = url          # a process variable wins over .env
    env["LLM_WARMUP"] = "false"        # never load the language model for database work
    env.pop("QJ_MYSQL_ADMIN_PASSWORD", None)
    return env


def app_connect_check(s, password):
    pymysql = import_pymysql()
    step(f"Checking that '{s['user']}' can log in to '{s['db']}'")
    try:
        conn = pymysql.connect(host=s["host"], port=s["port"], user=s["user"], password=password,
                               database=s["db"], connect_timeout=10)
        conn.close()
    except Exception as exc:
        fail(f"The application account could not connect ({exc.__class__.__name__}).",
             "See docs/DATABASE_SETUP_GUIDE.md, Troubleshooting C and F.")
    ok("Application account connected")


def repo_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        fail(f"The repository has {len(heads)} migration heads; expected exactly one.")
    return heads[0]


def run_migrations(url):
    step("Running the database migrations (flask db upgrade)")
    say("    This builds every Quick Junction table from the migration files. It can take a minute.")
    result = subprocess.run([sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade"],
                            cwd=ROOT, env=child_env(url), capture_output=True, text=True)
    applied = [ln.split("Running upgrade", 1)[1].strip() for ln in (result.stdout + result.stderr).splitlines()
               if "Running upgrade" in ln]
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
        for ln in tail:
            say("      " + ln)
        fail("flask db upgrade failed.", "Read the error above. See docs/DATABASE_SETUP_GUIDE.md, Troubleshooting I.")
    ok(f"Migrations finished ({len(applied)} applied in this run)")
    for a in applied:
        say("      " + a)


def verify(s, password, url):
    pymysql = import_pymysql()
    step("Verifying the database")
    head = repo_head()
    conn = pymysql.connect(host=s["host"], port=s["port"], user=s["user"], password=password, database=s["db"])
    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        rows = [r[0] for r in cur.fetchall()]
        if rows != [head]:
            fail(f"Database revision is {rows or 'missing'}, repository head is {head}.",
                 "Run: .venv\\Scripts\\python -m flask --app run.py db upgrade")
        ok(f"Migration revision {head} = repository head")

        sys.path.insert(0, str(ROOT))
        os.environ.setdefault("LLM_WARMUP", "false")
        from app.extensions import db as _db
        from app import models  # noqa: F401  (registers every table)
        expected = sorted(t for t in _db.metadata.tables)
        cur.execute("SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s", (s["db"],))
        found = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        missing = [t for t in expected if t not in found]
        if missing:
            fail("Tables missing after migration: " + ", ".join(missing))
        ok(f"All {len(expected)} application tables exist (+ alembic_version)")
        wrong = [t for t in expected if found[t][0] != "InnoDB" or not (found[t][1] or "").startswith("utf8mb4")]
        if wrong:
            fail("Tables not InnoDB/utf8mb4: " + ", ".join(wrong))
        ok("All tables are InnoDB with utf8mb4 text")
        cur.execute("SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
                    "WHERE TABLE_SCHEMA = %s AND CONSTRAINT_TYPE = 'FOREIGN KEY'", (s["db"],))
        fks = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
                    "WHERE TABLE_SCHEMA = %s AND CONSTRAINT_TYPE = 'CHECK'", (s["db"],))
        checks = cur.fetchone()[0]
        if fks == 0 or checks == 0:
            fail(f"Constraints look incomplete ({fks} foreign keys, {checks} CHECK constraints).")
        ok(f"{fks} foreign keys and {checks} CHECK constraints present")
        cur.execute("SELECT tax_rate, staff_max_discount FROM pricing_settings WHERE id = 1")
        row = cur.fetchone()
        if not row:
            fail("The pricing_settings row seeded by the migrations is missing.")
        ok(f"Pricing settings present (tax {row[0]}%, staff discount cap {row[1]}%)")
    conn.close()

    step("Starting Quick Junction against the database (smoke test)")
    result = subprocess.run([sys.executable, "-c",
                             "from app import create_app;"
                             "a = create_app('development');"
                             "c = a.test_client();"
                             "h = c.get('/health').status_code;"
                             "m = c.get('/menu').status_code;"
                             "print('HEALTH', h, 'MENU', m)"],
                            cwd=ROOT, env=child_env(url), capture_output=True, text=True)
    out = result.stdout.strip().splitlines()
    last = out[-1] if out else ""
    if result.returncode != 0 or last != "HEALTH 200 MENU 200":
        for ln in (result.stderr or result.stdout).strip().splitlines()[-8:]:
            say("      " + ln)
        fail("The application could not start or read from the database.")
    ok("Application started; /health and /menu answered 200 using the new database")


def maybe_seed(url):
    choice = os.environ.get("QJ_LOAD_DEMO")
    if choice is None:
        say("")
        say("    Demo data adds a sample menu, six tables and three DEMO accounts")
        say("    (customer / staff / admin, password demo-password-1). Development only.")
        wanted = confirm("Load demo data now?", default_yes=True)
    else:
        wanted = choice == "1"
    if not wanted:
        say("    Skipped demo data. You can load it later with: .venv\\Scripts\\python scripts\\seed_demo.py")
        return
    step("Loading demo data (scripts/seed_demo.py)")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo.py")], cwd=ROOT,
                            env=child_env(url), capture_output=True, text=True)
    if result.returncode != 0:
        for ln in (result.stderr or result.stdout).strip().splitlines()[-6:]:
            say("      " + ln)
        fail("Loading demo data failed.")
    ok("Demo data loaded (safe to run again; it only adds what is missing)")


def env_credentials(s):
    lines = read_env_lines() if ENV_FILE.exists() else []
    pw = existing_app_password(lines, s)
    if not pw:
        fail(f".env has no working DATABASE_URL for user '{s['user']}' and database '{s['db']}'.",
             "Run scripts\\setup_database.bat first.")
    return pw


# --- commands ---------------------------------------------------------------------------------
def cmd_setup():
    s = settings()
    say("")
    say(f"    Database: {s['db']}   Application account: {s['user']}   Server: {s['host']}:{s['port']}")
    if not confirm("Create (or update) the Quick Junction database and application account?", default_yes=True):
        say("    Nothing was changed.")
        return
    conn = admin_connect(s)
    lines = read_env_lines()
    known = existing_app_password(lines, s)
    password = known or secrets.token_urlsafe(24)
    with conn.cursor() as cur:
        ensure_database(cur, s)
        ensure_app_user(cur, s, password, known is not None)
    conn.close()
    update_env(s, password)
    url = app_url(s, password)
    app_connect_check(s, password)
    run_migrations(url)
    verify(s, password, url)
    maybe_seed(url)
    say("")
    say("    SUCCESS: the Quick Junction database is ready.")


def cmd_verify():
    s = settings()
    password = env_credentials(s)
    url = app_url(s, password)
    app_connect_check(s, password)
    verify(s, password, url)
    say("")
    say("    SUCCESS: the Quick Junction database is verified.")


def cmd_reset():
    s = settings()
    say("")
    say("    ********************************************************************")
    say("    *  DESTRUCTIVE: this permanently deletes the Quick Junction database *")
    say(f"    *  '{s['db']}' on {s['host']}:{s['port']} -- every order, payment,")
    say("    *  reservation, account and audit record in it. It cannot be undone.")
    say("    *  No other database on the server is touched.                      *")
    say("    ********************************************************************")
    try:
        typed = input(f"    Type {RESET_PHRASE} to continue: ")
    except EOFError:
        typed = ""
    if typed.strip() != RESET_PHRASE:
        say("    The phrase did not match. Nothing was deleted.")
        sys.exit(1)
    conn = admin_connect(s)
    lines = read_env_lines()
    known = existing_app_password(lines, s)
    password = known or secrets.token_urlsafe(24)
    with conn.cursor() as cur:
        step(f"Dropping and recreating '{s['db']}'")
        cur.execute(f"DROP DATABASE IF EXISTS `{s['db']}`")
        cur.execute(f"CREATE DATABASE `{s['db']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        ok(f"'{s['db']}' is now empty")
        ensure_app_user(cur, s, password, known is not None)
    conn.close()
    update_env(s, password)
    url = app_url(s, password)
    app_connect_check(s, password)
    run_migrations(url)
    verify(s, password, url)
    maybe_seed(url)
    say("")
    say("    SUCCESS: the Quick Junction database was reset and rebuilt.")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "setup"
    commands = {"setup": cmd_setup, "verify": cmd_verify, "reset": cmd_reset}
    if command not in commands:
        say("Usage: python scripts/db_setup.py [setup|verify|reset]")
        sys.exit(2)
    os.chdir(ROOT)
    commands[command]()
