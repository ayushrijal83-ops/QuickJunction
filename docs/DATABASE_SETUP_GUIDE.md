# Quick Junction — Database Setup Guide (Windows 11)

This guide takes a **completely fresh Windows 11 computer** to a running Quick Junction
installation with its own MySQL database. It is written for someone who has never set
up MySQL before. Follow the sections in order.

> **Short version.** Install Python 3.11 (section 3) and MySQL Server (section 4),
> copy the project (section 7), then double-click **`setup_quick_junction.bat`**
> (section 10). The script does sections 8–12 for you.

---

## 1. Purpose

After this guide you will have:

```text
Windows 11
   │
   ├── Python 3.11 (or 3.10)          the language Quick Junction is written in
   ├── MySQL Server 8                 the database server (runs as a Windows service)
   │
   └── Quick Junction folder
         ├── .venv                    private copy of the Python libraries
         ├── .env                     your local settings (database password lives here)
         │
         └── MySQL database "quick_junction"
               ├── account "qj_user"  used by the application (not root)
               └── 14 tables          built by the migration files (Alembic)
```

and a verified application that answers at <http://127.0.0.1:5000>.

This is a **development / local** installation. It is not a production deployment
(see section 20).

---

## 2. Requirements

| Software | Status | Version | Why |
| --- | --- | --- | --- |
| Windows 11 | **REQUIRED** | any edition | the scripts are Windows batch files |
| Python | **REQUIRED** | **3.11 or 3.10** (64-bit) | `requirements.txt` pins numpy 1.24.3, scipy 1.15.3 and scikit-learn 1.7.2, which need Python 3.10 or 3.11. **Python 3.12, 3.13 and newer will NOT work.** |
| MySQL Server | **REQUIRED** | **8.0** (tested: 8.0.46) — see note | stores all Quick Junction data |
| Internet connection | **REQUIRED** (during setup) | — | `pip` downloads about 100 MB of Python packages |
| Git | OPTIONAL | any | only if you clone the repository instead of copying the folder |
| MySQL Workbench | OPTIONAL | any | a graphical tool to look inside the database; not needed by the scripts |
| MySQL Shell | NOT REQUIRED | — | Quick Junction never uses it |
| Connectors (ODBC, .NET, J, Python connector) | NOT REQUIRED | — | Quick Junction uses its own driver, **PyMySQL**, installed by `pip` |
| AI model files (`models/`) + `requirements-ai.txt` | OPTIONAL | — | only for the AI explanation page; everything else works without them (README section 9) |

> **About the MySQL version.** Quick Junction was built and tested on **MySQL 8.0.46**.
> MySQL 8.0 reached end of life in April 2026 (8.0.46 is its final release), so the
> MySQL download page may now offer **MySQL 8.4 LTS** first. 8.4 uses the same default
> login method and SQL features the project relies on, and the setup script verifies
> the finished database either way — but 8.4 **has not been tested** with Quick
> Junction. If you want the exact tested version, choose **8.0.46** from the MySQL
> "Archives" tab on the download page.

---

## 3. Install Python

1. Open <https://www.python.org/downloads/windows/>.
2. Find the newest **Python 3.11.x** release and download the
   **Windows installer (64-bit)**. (3.10.x also works. Do **not** pick 3.12 or newer.)
3. Run the installer. On the **first screen**:
   - ✅ tick **"Add python.exe to PATH"** (bottom of the window) — **important**
   - ✅ leave **"Use admin privileges when installing py.exe"** ticked
   - click **Install Now**.
4. When it finishes, close the installer.

**Verify.** Open a **new** PowerShell window (Start → type *PowerShell* → Enter) and run:

```powershell
python --version
py --version
py -0
```

You should see `Python 3.11.x` (or 3.10.x). `py -0` lists every installed Python; the
setup script automatically prefers 3.11, then 3.10, so other versions on the same
computer are fine.

> If `python --version` opens the Microsoft Store instead, Python was not added to
> PATH: re-run the installer, choose **Modify**, and tick *Add Python to environment
> variables*. See troubleshooting item L.

---

## 4. Install MySQL Server

Download from <https://dev.mysql.com/downloads/mysql/> → **Microsoft Windows** →
**Windows (x86, 64-bit), MSI Installer**. You do **not** need an Oracle account: on the
download page choose **"No thanks, just start my download."**

The screens differ slightly between versions. The important settings are the same;
the wording may vary.

### 4a. MySQL 8.4 LTS (the MSI + MySQL Configurator)

1. Run the `.msi`. Setup type: **Typical** is enough (it installs MySQL Server and the
   `mysql` command-line client). Click **Install**.
2. At the end, leave **"Run MySQL Configurator"** ticked and click **Finish**.
3. In **MySQL Configurator**:

| Screen | What to choose | |
| --- | --- | --- |
| Data Directory | keep the default | REQUIRED (default is fine) |
| Type and Networking → Config type | **Development Computer** | recommended |
| TCP/IP | ✅ enabled, **Port 3306** | **REQUIRED** — Quick Junction connects over TCP to 127.0.0.1:3306 |
| X Protocol port (33060) | leave as shown | NOT used by Quick Junction |
| Open Windows Firewall ports for network access | **untick** | not needed for a local installation (see section 18) |
| Accounts and Roles → root password | choose a **strong password** and **write it down** | **REQUIRED** — the setup script asks for it once |
| Add user accounts | skip | the setup script creates `qj_user` for you |
| Windows Service | ✅ **Configure MySQL Server as a Windows Service**, keep the suggested name (for example **MySQL84**), ✅ **Start the MySQL Server at System Startup**, run as **Standard System Account** | **REQUIRED** |
| Server File Permissions | keep the default | |
| Sample databases | untick | NOT REQUIRED |
| Apply Configuration | click **Execute**, wait for all green ticks, then **Finish** | |

### 4b. MySQL 8.0 (the tested version, "MySQL Installer")

If you downloaded 8.0 from the Archives, the package is **MySQL Installer**:

1. **Choosing a Setup Type**: choose **Custom** (recommended) and add only
   **MySQL Server 8.0.x** (REQUIRED) and, if you want a graphical tool,
   **MySQL Workbench** (OPTIONAL). **Developer Default** also works but installs many
   extras you do not need (Shell, Router, Connectors, Documentation, Samples).
   **Server only** also works.
2. **Type and Networking**: Config Type **Development Computer**, ✅ TCP/IP,
   **Port 3306**, leave X Protocol Port as shown, **untick** "Open Windows Firewall port
   for network access".
3. **Authentication Method**: choose **"Use Strong Password Encryption for
   Authentication (RECOMMENDED)"**. Do **not** choose "Legacy Authentication" —
   Quick Junction does not need it.
4. **Accounts and Roles**: set the **root password** (write it down). Skip "MySQL User
   Accounts" — the setup script creates the application account.
5. **Windows Service**: ✅ Configure as a Windows Service, service name **MySQL80**,
   ✅ **Start the MySQL Server at System Startup**, Standard System Account.
6. **Apply Configuration** → **Execute** → **Finish**.

---

## 5. MySQL installation values

| Setting | Recommended value | Why |
| --- | --- | --- |
| MySQL Server port | **3306** | Quick Junction's default (`.env.example`, `DATABASE_URL`) |
| Host the application uses | **127.0.0.1** | the setup script and `.env` use TCP loopback |
| Authentication | **Strong password encryption** (`caching_sha2_password`, the MySQL 8 default) | works with PyMySQL; tested on 8.0.46 |
| Windows Service | **enabled** (MySQL80 / MySQL84) | MySQL must be running for Quick Junction to start |
| Start at system startup | **enabled** | avoids "Can't connect" after every reboot |
| Firewall port for network access | **not opened** | nothing outside your PC needs to reach MySQL |
| `root` account | created during installation, strong password | used **only once**, by the setup script, to create the database and application account |
| Application account | **`qj_user`**, created by the setup script | what Quick Junction logs in as every day |
| Database | **`quick_junction`**, utf8mb4, created by the setup script | Quick Junction's data |

---

## 6. Verify MySQL

Three different things are called "MySQL":

| Name | What it is |
| --- | --- |
| **MySQL Server** | the database program. It runs in the background as a Windows service. |
| **MySQL command-line client** (`mysql.exe`) | a text program you use to type SQL to the server. |
| **MySQL Workbench** | an optional graphical program that does the same as the client. |

**Is the server running?** Press **Win + R**, type `services.msc`, Enter. Find
**MySQL80** (or **MySQL84**). Status should be **Running**.

**Is the client installed?** In PowerShell:

```powershell
mysql --version
```

If you get *"mysql is not recognized"*, the client is installed but not on PATH. Use
the full path instead (adjust the version folder):

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" --version
```

**Connect to the server:**

```powershell
mysql -u root -p
```

It asks `Enter password:` — type the root password (nothing appears while you type).
The prompt changes to `mysql>`. **You are now inside MySQL.** Only now can you type
SQL:

```sql
SHOW DATABASES;
EXIT;
```

> ⚠ **PowerShell is not MySQL.** `SHOW DATABASES;` typed at the `PS C:\>` prompt fails
> with *"The term 'SHOW' is not recognized"*. SQL only works after `mysql -u root -p`,
> at the `mysql>` prompt. `EXIT;` takes you back to PowerShell.

---

## 7. Get the Quick Junction project

**Option A — Git clone** (needs Git):

```powershell
cd D:\
git clone <repository-url> QuickJunction
cd D:\QuickJunction
```

**Option B — copy the folder** from a USB drive or share to, for example,
`D:\QuickJunction`. Do **not** copy these from the old computer (they are machine
specific and are recreated): `.venv`, `.env`, `logs`, `__pycache__`.

The folder must contain `run.py`, `requirements.txt`, `.env.example`, `app\`,
`migrations\` and `scripts\`.

The AI model files (`models\`) are never in Git; copy them separately only if you want
the AI explanation page (README section 9).

---

## 8. Create the Python virtual environment

`setup_quick_junction.bat` does this for you. To do it by hand, in PowerShell:

```powershell
cd D:\QuickJunction
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

(`py -3.10` if you installed 3.10.) After activation the prompt starts with `(.venv)`.
The optional AI packages are in `requirements-ai.txt`; skip them unless you also copied
`models\`.

---

## 9. Configure environment variables (`.env`)

Quick Junction reads its settings from a file called **`.env`** in the project folder.
The setup script creates it from `.env.example` and fills in the database line and a
random `SECRET_KEY`. **`.env` is ignored by Git and must never be committed.**

What the important lines look like (placeholders — never put a real password in
documentation):

```ini
APP_ENV=development
SECRET_KEY=<a long random value — the setup script generates it>
DATABASE_URL=mysql+pymysql://qj_user:YOUR_PASSWORD@127.0.0.1:3306/quick_junction
```

| Part | Meaning |
| --- | --- |
| `mysql+pymysql` | MySQL, through the PyMySQL driver (installed from `requirements.txt`) |
| `qj_user` | the application account |
| `YOUR_PASSWORD` | its password (the setup script generates a 32-character one) |
| `127.0.0.1:3306` | your own computer, MySQL port |
| `quick_junction` | the database |

**Special characters in the password.** The password sits inside a URL, so characters
such as `@ : / ? # % &` must be *percent-encoded* (`@` → `%40`, `#` → `%23`, `%` → `%25`).
The password the setup script generates only contains letters, digits, `-` and `_`, so
it never needs encoding. If you set your own, encode it with:

```powershell
.\.venv\Scripts\python -c "from urllib.parse import quote_plus; print(quote_plus(input('password: ')))"
```

> A variable already set in the Windows environment wins over the same line in `.env`.
> If Quick Junction seems to ignore your `.env`, see troubleshooting item M.

---

## 10. Create the database (recommended: the setup script)

**Easiest — everything at once.** Double-click **`setup_quick_junction.bat`** in the
project folder (or run it from PowerShell: `.\setup_quick_junction.bat`). It:

1. checks Windows, Python 3.10/3.11 and MySQL (PATH, the standard install folder and the
   Windows service; if it cannot find MySQL it asks for the path to `mysql.exe`);
2. creates `.venv` and installs `requirements.txt`;
3. asks for the **MySQL root username** (Enter = `root`) and **root password** (typing
   is hidden) — used only to create the database and the two accounts below, never saved;
4. creates the database `quick_junction` (utf8mb4) **if it does not exist** — an
   existing database is kept, nothing is dropped;
5. creates **two** accounts, each as `'...'@'127.0.0.1'` and `'...'@'localhost'`, with
   privileges on `quick_junction` only (M7.3 — database privilege separation):
   - `qj_user` — the **runtime** account, data access only (`SELECT, INSERT, UPDATE,
     DELETE`), no schema privileges;
   - `qj_migrator` — the **migration** account, used only for `flask db upgrade`/
     `downgrade`, with data access *and* schema privileges;
6. writes `DATABASE_URL` (the **runtime** account only) and `SECRET_KEY` to `.env` — the
   migration account's URL and password are never written to `.env` or any other file,
   only held in memory for this one setup run and passed to the migration step as the
   `MIGRATION_DATABASE_URL` environment variable;
7. runs every migration **as `qj_migrator`**, verifies the result (as `qj_user`), starts
   the application once as a smoke test, and offers to load demo data.

It never starts the application for you, and it is **safe to run again**: the second
time it finds everything in place, applies 0 migrations and leaves `.env` unchanged. It
also never changes an existing installation's grants — see section 10.1 if you set this
database up before M7.3 and want to narrow `qj_user` to drop its old schema privileges.

If `.venv` already exists, **`scripts\setup_database.bat`** runs only the database part.

**What the script runs (for learning — you do not need to type this).** Inside
`mysql -u root -p`:

```sql
CREATE DATABASE IF NOT EXISTS quick_junction
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Runtime account: what the application itself uses, in DATABASE_URL.
CREATE USER 'qj_user'@'127.0.0.1' IDENTIFIED BY 'YOUR_PASSWORD';
CREATE USER 'qj_user'@'localhost' IDENTIFIED BY 'YOUR_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE
  ON quick_junction.* TO 'qj_user'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE
  ON quick_junction.* TO 'qj_user'@'localhost';

-- Migration account: used only for `flask db upgrade`/`downgrade`, via
-- MIGRATION_DATABASE_URL. Never used by the running application.
CREATE USER 'qj_migrator'@'127.0.0.1' IDENTIFIED BY 'ANOTHER_PASSWORD';
CREATE USER 'qj_migrator'@'localhost' IDENTIFIED BY 'ANOTHER_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON quick_junction.* TO 'qj_migrator'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON quick_junction.* TO 'qj_migrator'@'localhost';
```

Why these privileges: the running application only ever does `SELECT`/`INSERT`/
`UPDATE`/`DELETE` — verified against the actual code, not assumed: nothing under `app/`
executes `CREATE`/`ALTER`/`DROP`/`LOCK TABLES` at runtime, and `SELECT ... FOR UPDATE`
(used throughout the order/payment/reservation code for row locking) needs only
`SELECT`, not a table lock. `qj_migrator` needs `CREATE/ALTER/DROP/INDEX/REFERENCES`
for `flask db upgrade`'s schema changes, *and* keeps `SELECT/INSERT/UPDATE/DELETE`
because several migrations back-fill or clean up data as part of a schema change —
e.g. `7c4e1a9b52d3` grandfathers pre-existing staff accounts with an `UPDATE`, and
several others `DELETE` rows that would violate a widened `CHECK` constraint. Nothing
outside `quick_junction` for either account, and no server-wide rights such as
`CREATE USER`, `GRANT` or `FILE`. Both host forms exist because MySQL treats
`127.0.0.1` and `localhost` as different accounts.

### 10.1 Upgrading an installation set up before M7.3

If this database was created before the privilege split, `qj_user` still holds the old
combined privileges (`GRANT` is additive — re-running `setup`/`reset` adds `qj_migrator`
but never removes anything `qj_user` already had). To narrow it deliberately:

```powershell
.\.venv\Scripts\python scripts\db_setup.py narrow-runtime
```

This asks for the admin password and a typed confirmation phrase (`NARROW QJ_USER`,
the same pattern as `reset`'s own destructive-action confirmation), then revokes every
privilege `qj_user` currently holds and re-grants only `SELECT, INSERT, UPDATE, DELETE`.
It never touches `qj_migrator` or any other account or database, and it is never run
automatically — an existing installation's grants only change when you ask for this
explicitly. Run `python scripts\db_setup.py verify` afterward to confirm the application
still works with the narrowed account.

---

## 11. Run database migrations

Quick Junction never creates tables by hand. Its schema lives in **migration files**:

```text
migrations\versions\*.py   (11 files, oldest → newest)
        │
        ▼   Alembic, run through Flask-Migrate:  flask db upgrade
        │
MySQL database quick_junction   (14 tables + alembic_version)
```

The setup script runs this for you, as the `qj_migrator` account. To run it yourself
(PowerShell, project folder) with the migration account, set
`MIGRATION_DATABASE_URL` for just this one command:

```powershell
$env:MIGRATION_DATABASE_URL = "mysql+pymysql://qj_migrator:YOUR_MIGRATOR_PASSWORD@127.0.0.1:3306/quick_junction"
.\.venv\Scripts\python -m flask --app run.py db upgrade
Remove-Item Env:MIGRATION_DATABASE_URL
```

`DATABASE_URL` in `.env` (the runtime account) is still required — the application
itself needs it to start — but when `MIGRATION_DATABASE_URL` is set, `migrations\env.py`
connects with it instead for the actual schema change. **Backward compatibility:** if
you never set `MIGRATION_DATABASE_URL`, migrations run against `DATABASE_URL` exactly
as they did before M7.3 — this is the fallback an installation that has not split its
accounts yet keeps relying on:

```powershell
.\.venv\Scripts\python -m flask --app run.py db upgrade
```

Check where the database is, and what the newest migration in the project is:

```powershell
.\.venv\Scripts\python -m flask --app run.py db current
.\.venv\Scripts\python -m flask --app run.py db heads
```

Both must print the same revision. At the time of writing the head is
**`f3a6d9b2e8c5`** ("add kitchen preparation timestamps to orders"). The full chain,
from empty:

```text
abf997064564  users and audit_logs
8d5ba0171efe  menu data layer (+ CHECK constraints)
d8f3bfd0e2a9  cart / orders
38297b707b89  audit events for order status
2d9f3b20045f  users.session_version (session revocation)
7c4e1a9b52d3  users.staff_approved
b5e2f8c41a07  restaurant tables, reservations, order source, cancellation
c3a9d7e21f58  payments
d7f1e3a9c2b4  tax and discount pricing (seeds pricing_settings: tax 13%, staff cap 20%)
e5b8c1d4f7a2  inventory and stock movements
f3a6d9b2e8c5  kitchen preparation timestamps        ◄ head
```

> Never delete or edit files in `migrations\`. Never create Quick Junction tables by
> hand — Alembic would not know about them.

---

## 12. Verify the database

The quickest complete check (connection, head, tables, constraints, application start):

```powershell
.\.venv\Scripts\python scripts\db_setup.py verify
```

Expected: a list of `[OK]` lines ending in `SUCCESS: the Quick Junction database is
verified.` It checks that:

- `qj_user` (the runtime account) can log in to `quick_junction`;
- the database revision equals the repository head;
- all 14 application tables exist, InnoDB, utf8mb4;
- foreign keys and CHECK constraints exist, and the `pricing_settings` row is there;
- the application starts and `/health` and `/menu` answer 200 using the database.

`verify` only checks the runtime account — it does not run migrations, so it never
touches `qj_migrator`.

To look for yourself, inside `mysql -u root -p`:

```sql
SHOW DATABASES;
USE quick_junction;
SHOW TABLES;
SELECT version_num FROM alembic_version;
SHOW GRANTS FOR 'qj_user'@'127.0.0.1';
SHOW GRANTS FOR 'qj_migrator'@'127.0.0.1';
```

The first `SHOW GRANTS` should list only `SELECT, INSERT, UPDATE, DELETE`; the second
should additionally list `CREATE, ALTER, DROP, INDEX, REFERENCES`. If `qj_user`'s grants
also include schema privileges, this installation predates M7.3 — see section 10.1.

### 12.1 Check the installation identity (O1)

Migration `4c7e2a9f1b38` gives every database -- new or upgraded -- one
Organization, one Branch and one Installation identity, each with a UUID generated
locally when the migration runs (no internet needed). Both names are copied from
the restaurant name in the restaurant profile. Nothing extra has to be run: `flask db
upgrade` (or `scripts\db_setup.py setup`) creates it. To see it:

```powershell
.\.venv\Scripts\python -m flask --app run.py installation show
```

Expected (your UUIDs differ):

```text
Organization:       Quick Junction
  Public ID:        17daf2d6-2112-4a55-99e4-30919d6209b1
  HQ ID:            not linked
Branch:             Quick Junction
  Public ID:        27062fd3-4dbc-42f5-82aa-37385581f8fa
  HQ ID:            not linked
Installation ID:    a7b9a239-0861-4a33-aac2-c0f717c1779b
  Created:          2026-11-13 01:28:24
HQ linked:          no
```

The command only reads. If it reports that no identity was found, the database is
not fully migrated (run `flask db upgrade`) or was restored from an incomplete copy;
it never creates or repairs one by itself. Renaming the restaurant later does not
change the identity, and backups (section 19) preserve its UUIDs exactly.

**Downgrades remove the identity.** Downgrading `4c7e2a9f1b38` drops the
`organizations`, `branches` and `installation` tables, and with them this
installation's identity. Upgrading again generates **new** UUIDs; only restoring a
backup brings the original ones back.

This can happen even when the downgrade command *fails*. Alembic downgrades one
revision at a time, newest first, so when you downgrade to a revision older than
`4c7e2a9f1b38`, its downgrade runs -- and removes the identity -- before the older
revisions'. If one of those older revisions then refuses (for example
`e39e6dffcb5d` refuses while the payment ledger holds adjustment or correction
entries), the command reports an error, but the identity tables are already gone
and the database is left at the revision just below `4c7e2a9f1b38`, without its
local identity. A later `flask db upgrade` creates a new identity with new UUIDs.

Before attempting **any** downgrade, take a backup and verify it (section 19), so
the original identity can be restored if needed. Check `flask installation show`
afterwards.

Optional: the automated test suite (uses its own in-memory SQLite database, never your
MySQL data):

```powershell
.\.venv\Scripts\python -m pytest -q
```

On a fresh copy without `models\`, 725 tests are collected and all pass except skips:
7 MySQL-only concurrency tests and 5 tests that need the AI model files.

---

## 13. Start Quick Junction

```powershell
cd D:\QuickJunction
.\.venv\Scripts\python run.py
```

Open **<http://127.0.0.1:5000>** in a browser. Stop it with **Ctrl+C** in the
PowerShell window. MySQL must be running (section 6).

The first lines of the log should **not** contain *"DATABASE_URL connects as MySQL
'root'"* — if they do, your `.env` still uses root (see section 18).

---

## 14. First login / admin setup

Quick Junction has three sign-in pages: customers at `/login`, staff at `/login/staff`,
administrators at `/login/admin`.

- **Customers** register themselves at `/register`.
- **Staff** request an account at `/register/staff`; an administrator must approve it
  (Admin → Staff accounts) before it can sign in.
- **Administrators cannot sign up.** There is no public admin registration by design.

**Getting the first administrator:**

1. **`flask create-admin` (use this on every real installation).** From the project
   folder, in an interactive terminal, after `flask db upgrade`:

   ```powershell
   .\.venv\Scripts\python -m flask --app run.py create-admin
   ```

   It asks for a username, an email and the password twice (the password is not
   shown while typing, and it is never accepted as a command-line argument or from
   piped input). It applies the same username, email and password rules as web
   registration, refuses an existing username or email, stores only the Argon2id
   hash, and records an `admin_created` event in the audit log. On success it prints
   only the username. Use it to create the first administrator of a new
   installation, or another administrator if none can sign in; it never changes an
   existing account. Exit status is 1 if nothing was created.

2. **Demo data (development only).** Answer **Y** to "Load demo data now?" during
   setup, or run later:

   ```powershell
   .\.venv\Scripts\python scripts\seed_demo.py
   ```

   It adds a sample menu, six tables and three demo accounts — `customer`, `staff`,
   `admin` — all with the password **`demo-password-1`**. That password is public (it
   is in the repository), so use these accounts only on your own computer. The script
   refuses to run in a production configuration and is safe to run more than once.

Do not promote an account to `admin` with SQL: it bypasses the password rules and
leaves no audit record.

---

## 15. Troubleshooting

**A. "mysql is not recognized as the name of a cmdlet…"**
*Meaning:* the MySQL client is not on PATH (MySQL may still be installed).
*Check:* `Test-Path "C:\Program Files\MySQL"`.
*Fix:* use the full path, e.g. `& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u root -p`,
or add that `bin` folder to PATH (Start → "Edit the system environment variables" →
Environment Variables → Path → New). The setup script does not need `mysql` on PATH.

**B. MySQL service is not running**
*Meaning:* the server is installed but stopped.
*Check:* `Get-Service MySQL*` in PowerShell.
*Fix:* `services.msc` → MySQL80/MySQL84 → **Start**, and set Startup type to
**Automatic**. Or in an **Administrator** PowerShell: `net start MySQL80`.

**C. "Access denied for user 'root'@'localhost'" (error 1045)**
*Meaning:* wrong username or password.
*Check:* `mysql -u root -p` with the password you wrote down during installation.
*Fix:* re-type carefully (keyboard layout, Caps Lock). If the root password is lost,
follow the official "How to Reset the Root Password" page for your MySQL version.
If the denied user is **`qj_user`**, run `scripts\setup_database.bat` again — it
re-applies the password stored in `.env` and the grants.

**D. "Can't connect to MySQL server on '127.0.0.1:3306'" (error 2003)**
*Meaning:* nothing is listening on that host/port.
*Check:* item B; then `Test-NetConnection 127.0.0.1 -Port 3306` (TcpTestSucceeded
should be True).
*Fix:* start the service; if MySQL uses another port, put that port in `DATABASE_URL`.

**E. "Unknown database 'quick_junction'" (error 1049)**
*Meaning:* the database was never created (or has another name).
*Check:* `SHOW DATABASES;` inside MySQL.
*Fix:* run `scripts\setup_database.bat`. If your database has a different name, make
the last part of `DATABASE_URL` match it.

**F. DATABASE_URL is incorrect**
*Meaning:* errors such as *"Could not parse SQLAlchemy URL"*, or *"missing required
environment variable(s): DATABASE_URL"*.
*Check:* open `.env` in Notepad; the line must look exactly like section 9, on one
line, no spaces, no quotes.
*Fix:* re-run `scripts\setup_database.bat` to rewrite it, and percent-encode special
characters if you typed your own password (section 9).

**G. "No module named 'pymysql'" (or flask, sqlalchemy …)**
*Meaning:* packages are not installed in the Python you are using.
*Check:* are you using `.venv\Scripts\python`? Plain `python` is the system Python.
*Fix:* `.\.venv\Scripts\python -m pip install -r requirements.txt`.

**H. "flask is not recognized" / "No such command 'db'"**
*Meaning:* Flask is run outside the virtual environment, or without the app.
*Fix:* always use the form `.\.venv\Scripts\python -m flask --app run.py db upgrade`
from the project folder.

**I. Migration errors / revision mismatch**
*Meaning:* `db current` differs from `db heads`, or `db upgrade` stopped part-way.
*Check:* `.\.venv\Scripts\python -m flask --app run.py db current` and `… db heads`.
*Fix:* run `… db upgrade` again and read the first error line. On a **new, empty**
database that still fails, and only if there is no data you need,
`scripts\reset_database.bat` rebuilds it (destructive — section 19 first). Never
delete migration files. Note: a full *downgrade* to an empty schema is known not to
work on MySQL for the three earliest migrations (project issue #32); upgrading is
unaffected.

**J. Port 3306 already in use**
*Meaning:* another MySQL/MariaDB (for example from XAMPP) already uses it.
*Check:* `Get-NetTCPConnection -LocalPort 3306 -State Listen` then look up the process
with `Get-Process -Id <OwningProcess>`.
*Fix:* stop the other server, or configure MySQL on another port (e.g. 3307) and set
that port in `DATABASE_URL`; for the setup script run
`$env:QJ_DB_PORT="3307"` in the same PowerShell window before `.\scripts\setup_database.bat`.

**K. PowerShell and SQL confused**
*Meaning:* *"The term 'SHOW' is not recognized"* (SQL typed into PowerShell) or
*"You have an error in your SQL syntax"* after typing `python …` at `mysql>`.
*Fix:* SQL only at `mysql>` (after `mysql -u root -p`); PowerShell commands only at
`PS …>`. Type `EXIT;` to leave MySQL.

**L. Virtual environment activation problems**
*Meaning:* *"running scripts is disabled on this system"* when running `Activate.ps1`.
*Fix:* you do not have to activate — use `.\.venv\Scripts\python …` directly. To
allow activation for your user:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. If `.venv` was copied from
another computer it will not work; delete the folder and run
`setup_quick_junction.bat` again.

**M. `.env` is not being loaded / old settings are used**
*Meaning:* the file is named wrongly (e.g. `.env.txt`) or a Windows environment
variable overrides it.
*Check:* in File Explorer enable *View → Show → File name extensions*; in PowerShell
`Get-ChildItem Env:DATABASE_URL`.
*Fix:* rename to exactly `.env` in the project folder; remove an overriding variable
with `$env:DATABASE_URL = $null` (current window) or from System → Environment
Variables.

**N. MySQL password contains special characters**
*Meaning:* login fails even though the password is right, or the URL cannot be
parsed.
*Fix:* percent-encode it in `DATABASE_URL` (section 9). The setup script's generated
passwords never need encoding. The root password is typed at a prompt and is never
put in a URL, so it may contain anything.

---

## 16. Clean machine checklist

```text
[ ] Windows 11 installed and updated
[ ] Python 3.11 (or 3.10) installed, "Add python.exe to PATH" ticked
[ ] Git installed (only if cloning)
[ ] MySQL Server 8 installed, port 3306, root password written down
[ ] MySQL service running and set to start automatically
[ ] MySQL verified (mysql -u root -p  →  SHOW DATABASES;)
[ ] Quick Junction copied or cloned (run.py is in the folder)
[ ] Virtual environment created (.venv)          ← setup_quick_junction.bat
[ ] Dependencies installed (requirements.txt)    ← setup_quick_junction.bat
[ ] .env configured                              ← setup_quick_junction.bat
[ ] Database quick_junction created              ← setup_quick_junction.bat
[ ] Application user qj_user created             ← setup_quick_junction.bat
[ ] Migrations completed                         ← setup_quick_junction.bat
[ ] Migration head verified (f3a6d9b2e8c5)       ← setup_quick_junction.bat
[ ] Application starts (.venv\Scripts\python run.py)
[ ] Browser login works (http://127.0.0.1:5000)
```

---

## 17. Database architecture

- **DBMS:** MySQL 8, InnoDB tables, utf8mb4 text (menu names and the rupee sign need
  more than basic Latin).
- **ORM:** SQLAlchemy 2 through Flask-SQLAlchemy; all queries are parameterised.
- **Driver:** PyMySQL (`mysql+pymysql://` in `DATABASE_URL`).
- **Migrations:** Alembic through Flask-Migrate (`migrations\`), 11 revisions, one head.
- **14 tables, in groups:**
  - accounts and security — `users`, `audit_logs`;
  - menu and inventory — `categories`, `menu_items`, `ingredients`,
    `menu_item_ingredients` (recipes), `stock_movements` (the stock ledger);
  - customers — `customer_preferences`;
  - ordering and money — `orders`, `order_items`, `payments`, `pricing_settings`;
  - floor — `restaurant_tables`, `reservations`.
- **Relationships:** an order belongs to a user and optionally a table, has order
  items and at most one payment; reservations link users and tables; stock movements
  link ingredients and (for sales) orders. Foreign keys use `ON DELETE RESTRICT`
  (except order items, which cascade with their order).
- **Integrity in the database itself:** CHECK constraints (e.g. price > 0, refund ≤
  payment, order total = subtotal − discount + tax), unique keys (one payment per
  order; no two live bookings of the same table and slot; one stock deduction per
  order and ingredient).
- **Audit/security:** `audit_logs` records sign-ins, status changes, payments, refunds,
  discounts and stock movements with the acting user and IP address — treat database
  backups as personal data.

Full details: `docs/DATABASE.md` and `docs/MYSQL_SETUP_HANDOFF.md` (section 10).

---

## 18. Security notes

- **Do not use MySQL `root` as the application account.** `root` can drop every
  database on the server. Quick Junction logs a warning when `DATABASE_URL` uses root,
  and refuses to start with `APP_ENV=production`. The setup script always uses
  `qj_user`.
- **Use the dedicated `qj_user` account** with privileges on `quick_junction` only.
  For a real deployment, run migrations with a separate account and narrow `qj_user`
  to `SELECT, INSERT, UPDATE, DELETE` (`docs/MYSQL_SETUP_HANDOFF.md` section 5.5).
- **Never commit `.env`.** It holds the database password and `SECRET_KEY`. It is in
  `.gitignore`; check with `git status` that it never appears.
- **Never put passwords in Git**, in documentation, or in screenshots. Never upload
  production credentials to GitHub.
- **Use strong passwords.** The setup script generates a random 32-character password
  for `qj_user`. Give `root` a strong password too.
- **Keep MySQL updated** with security releases for your version line.
- **Do not expose port 3306.** Leave "open firewall port for network access" unticked;
  Quick Junction connects over `127.0.0.1` and nothing else needs the port.

---

## 19. Backup and restore

Milestone 7.2 added a backup/restore tool (`scripts/backup_database.py` and
`scripts/restore_database.py`). **Backups are still not made automatically**
-- nothing runs them on a schedule unless an operator sets that up
explicitly (see "Scheduling" below). Before a reset, an upgrade or moving
computers, make one.

### Architecture

A backup is a `mysqldump --single-transaction --routines
--default-character-set=utf8mb4` dump of the database named in
`DATABASE_URL` (the same flags this section documented by hand before M7.2
-- a full structural + data dump, consistent for InnoDB without locking the
live application). The tool then:

1. Verifies the dump is actually complete -- not just that `mysqldump`
   returned 0 -- by checking for the header line every dump starts with and
   the completion line MySQL only ever writes as the very last line of a
   clean dump. A truncated dump (killed process, full disk) is rejected
   here, before it is ever written under a name that looks like a real
   backup.
2. Encrypts it at rest by default with Fernet (AES-128-CBC +
   HMAC-SHA256, from the maintained `cryptography` package -- nothing
   hand-rolled; see "Encryption" below).
3. Writes it **atomically**: to a temp file in the backup directory, then
   `os.replace()`s it into its final name. A crash mid-write never leaves a
   half-written file under the final name.
4. Computes a SHA-256 checksum of the final (encrypted) file and records it,
   alongside the database name, timestamp, file size, the `mysqldump`
   version, and a per-table row count snapshot, in a `.json` metadata file
   next to the backup -- `scripts/_backup_common.py:BackupMetadata`.
5. Applies retention: deletes the oldest backups of that database beyond a
   configurable count, but **never deletes the last remaining backup**, even
   if retention is configured as 0.

The restore tool (`scripts/restore_database.py`) verifies the backup's
checksum against its metadata **before touching any database** -- a
corrupted or tampered backup is refused at that point, with no connection
attempted. It then connects to the target and, before running anything
through `mysql`, refuses unconditionally if the target already has any
tables at all (see "Restore procedure" below -- this is the actual
protection against overwriting existing data; it does not trust the
database name or the production-confirmation flags). Only then does it
restore, and afterwards it re-checks every table's row count against what
was recorded at backup time, reporting any mismatch.

mysqldump invocations from M7.2's correction pass (2026-11-02 onward) also
pass `--skip-add-drop-table`, so a freshly made backup's SQL contains no
`DROP TABLE IF EXISTS` statements -- a second, independent layer: even if
the empty-target check above were ever bypassed by a code change, restoring
such a backup into a database with a same-named table now fails with a
plain MySQL "table already exists" error instead of silently replacing it.
Backups made before this change do still contain `DROP TABLE IF EXISTS`;
the empty-target check is what actually protects them, not the dump's own
content.

Install the one extra dependency these two scripts need:

```powershell
pip install -r requirements-ops.txt
```

(Not required to run the web app, the test suite, or any other script --
only backup/restore encryption needs it.)

### Configuration

Everything is an optional environment variable; sensible defaults apply if
none are set.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | (from `.env`) | What `backup_database.py` backs up. `restore_database.py` uses it only for host/port/credentials -- the database name always comes from `--target` |
| `QJ_BACKUP_DIR` | `..\quickjunction_backups` (**outside** the repository, one level above the project folder -- see "Never store backups inside the Git repository" below) | Where backup files and their metadata are written |
| `QJ_BACKUP_ENCRYPTION_KEY_FILE` | none | Path to a Fernet key file (see "Encryption-key management") |
| `QJ_BACKUP_RETENTION_COUNT` | `7` | How many backups of one database to keep |
| `QJ_MYSQLDUMP_PATH` / `QJ_MYSQL_PATH` | auto-detected (PATH, then the standard `Program Files\MySQL\MySQL Server *` install folder -- the same search `scripts\find_mysql.bat` already does) | Override the full path to `mysqldump.exe` / `mysql.exe` |

**M7.3 note (database privilege separation).** `backup_database.py` continues to use
`DATABASE_URL` -- the **runtime** account (`qj_user`), not `qj_migrator` -- and needs
nothing beyond what it already has: `mysqldump --single-transaction --routines` is
`SELECT`-only (there are no stored procedures, functions, views or triggers in this
schema today, so `--routines` has nothing to dump), and so is the row-count metadata
query.

**Restoring needs more than `qj_user` has, once narrowed.** `restore_database.py`
replays a dump's `CREATE TABLE` statements through `mysql`, which needs `CREATE` on the
(empty) target -- a privilege the runtime account no longer has after
`narrow-runtime`. Because `qj_migrator`'s password is deliberately never persisted
anywhere (see `docs/MYSQL_SETUP_HANDOFF.md` §5.5), there is no password to simply
"pass in" from memory. Add `--rotate-migration-credentials`:

```powershell
python scripts\restore_database.py --backup <path> --target quick_junction_test --rotate-migration-credentials
```

This asks for the MySQL admin password (or reads `QJ_MYSQL_ADMIN_USER`/
`QJ_MYSQL_ADMIN_PASSWORD`, the same convention `scripts\db_setup.py` already uses),
uses it to set `qj_migrator`'s password to a **fresh, one-time value**, restores with
that, then rotates the password away again to a second value nobody keeps --
regardless of whether the restore succeeded. Every other M7.2 safety check is
completely unchanged: the checksum is still verified before anything connects, the
target must still be empty, a production target still needs
`--allow-production-target` and the exact typed confirmation, system schemas are still
refused unconditionally. The only thing this flag changes is *how the database URL used
for the restore is obtained* -- it never touches `qj_user`'s grants, and the minted
migrator password is never printed or logged. See
`docs/MYSQL_SETUP_HANDOFF.md` §5.9 for the full account model and
`restore_backup_with_rotated_credentials()` in `scripts/restore_database.py` for the
exact mechanism. Restoring into an installation that has **not** been narrowed still
works exactly as M7.2 documented, without this flag.

**Never store backups inside the Git repository.** The default backup
directory is a sibling of the project folder, not a subdirectory of it, so
this holds even if `.gitignore` is ever misconfigured. `.gitignore` also
excludes `/backups/`, `*.sql.enc`, and this tool's own plaintext-backup file
name shape (`<database>_<timestamp>.sql`, matched narrowly so an unrelated
`.sql` file such as a schema or fixture is not also hidden) as a second
layer of protection, in case `QJ_BACKUP_DIR` is ever pointed inside the
repository -- in any subdirectory -- anyway. This matters more now that
`--no-encryption` is a documented flag: a plaintext backup made that way is
just as sensitive as an encrypted one and needs the same protection.

### Encryption-key management

```powershell
python scripts\backup_database.py --generate-key C:\secure\qj_backup.key
```

Creates a new Fernet key and refuses to overwrite an existing file. **Store
this key somewhere other than the backup directory and other than this
repository** -- a USB key, a password manager, a secrets vault. Losing it
makes every backup encrypted with it permanently unrecoverable; there is no
escrow or recovery path (a known limitation -- see below). The key itself is
never written into a backup file or its metadata; only a SHA-256
*fingerprint* of the key is recorded, so a restore can tell you "this is not
the right key" without the key ever being readable from anything that gets
backed up.

For local development only, `--no-encryption` writes a plain `.sql` backup
with no key required.

### Running a backup

```powershell
python scripts\backup_database.py
```

Uses `DATABASE_URL` from `.env`, the default backup directory, and
`QJ_BACKUP_ENCRYPTION_KEY_FILE` if set (otherwise pass `--key-file`
explicitly, or `--no-encryption`). On success it prints the backup's file
name, size and checksum; on any failure it prints what went wrong and exits
non-zero, having left nothing under a completed-looking name.

### Scheduling

The tool does not register itself as a scheduled task -- that is a
deliberate operator action, not something that happens silently. Set it up
once, on the real installation:

**Windows (Task Scheduler)**, daily at 02:00, running as the service account
the application itself runs under:

```powershell
schtasks /create /tn "QuickJunction-Backup" /tr "D:\Projects\QuickJunction\.venv\Scripts\python.exe D:\Projects\QuickJunction\scripts\backup_database.py" /sc daily /st 02:00 /ru SYSTEM
```

**Linux (cron)**, same schedule:

```
0 2 * * * /opt/quickjunction/.venv/bin/python /opt/quickjunction/scripts/backup_database.py >> /var/log/quickjunction-backup.log 2>&1
```

Either way, set `QJ_BACKUP_ENCRYPTION_KEY_FILE` (and the other
`QJ_BACKUP_*` variables, if not using the defaults) in the environment that
scheduled task/cron job runs under -- not on the command line, so the path
never appears in a process list or a scheduler's own log of what it ran.

**What this does not include**: success/failure alerting. `docs/PRODUCTION_READINESS.md`
item P1 tracks that as still open.

### Retention policy

`QJ_BACKUP_RETENTION_COUNT` (default 7) backups are kept per database;
older ones are deleted automatically after each successful backup. This is
**count-based, not calendar-based** -- "7" means the 7 most recent backups,
regardless of how many days that spans. The tool is guaranteed to never
delete the last remaining backup of a database, even if retention is
configured as 0 -- there is always at least one to restore from. This is
backup retention only; it has nothing to do with audit-log retention
(tracked separately as known issue #8).

### Restore procedure

**Into a disposable database (safe, the default mode)** -- the target name
must end in `_test`:

```powershell
python scripts\restore_database.py --backup <path-to-.sql-or-.sql.enc> --target quick_junction_test --key-file C:\secure\qj_backup.key
```

The target database must **already exist** (the tool never creates one --
create it the same way section 10 creates the main database, just with a
`_test`-suffixed name and the application account granted on it).

**Into a real/production database** -- deliberately harder, on purpose:

```powershell
python scripts\restore_database.py --backup <path> --target quick_junction --allow-production-target --confirm "RESTORE quick_junction" --key-file C:\secure\qj_backup.key
```

Both `--allow-production-target` and a `--confirm` string that exactly
matches `"RESTORE <target name>"` are required -- there is no shorter flag
or environment variable that skips this, and it has to be typed out for the
specific target database every time, the same pattern
`scripts\reset_database.bat` already uses for its own destructive action.
Restoring into any of MySQL's own system schemas (`mysql`, `sys`,
`information_schema`, `performance_schema`) is refused unconditionally, with
no override.

**The target must be completely empty (no tables at all), for every target,
with no override.** Immediately before running anything through `mysql`,
the tool runs `SHOW TABLES` against the target and refuses if it returns
even one table -- `_test`-suffixed or not, and regardless of
`--allow-production-target`/`--confirm`. This check does not depend on the
target's name and cannot be bypassed by any flag; if it fails, create a
genuinely empty database for the restore (the same way section 10 creates
the main one) or drop the existing tables yourself first, deliberately,
outside this tool. **Known limitation**: this is a check, not a lock -- if
something else creates a table in the target in the brief window between
this check and the restore that follows, that race is not caught. The
target this tool expects is one handed to it exclusively for the restore,
not a database something else is concurrently writing to.

Either way, the tool verifies the backup's checksum (and, if encrypted, that
the given key actually matches) **before** attempting to connect to the
target at all, confirms the target is empty, then restores, then reports a
per-table row-count comparison against what was recorded at backup time. A
`mysqldump`/`mysql` subprocess that does not finish within 3600 seconds is
aborted and reported as a clear, operator-facing error rather than a raw
traceback; for a restore, that error says plainly that the target may
already be partially restored, since `mysql` applies statements
sequentially and a killed process does not roll back ones already run. If
the post-restore row-count check itself hits a MySQL error (for example a
table that was renamed or dropped between backup time and restore time),
that is also reported as a clear error that says the restore already
completed before the check failed -- never silently treated as if nothing
happened.

### Recovery limitations

- **No key escrow.** One lost key file is unrecoverable data for every
  backup encrypted with it. Store it somewhere durable and separate from the
  server.
- **No off-machine copies yet.** The default backup location is a different
  folder, not a different machine -- a single-disk failure that takes the
  server down takes the backups with it unless an operator copies them
  elsewhere. Tracked as `docs/PRODUCTION_READINESS.md` item P2.
- **No automated/unattended restore drills.** The tool and its test
  (`tests/test_backup_restore.py::test_live_backup_and_restore_drill`) make
  an automated drill possible, but nothing runs it periodically yet -- see
  item P4.
- **Target database must pre-exist and be completely empty, for every
  target.** This is enforced at restore time (a `SHOW TABLES` check, see
  "Restore procedure" above) -- it is not just a convention, and not
  something a corrected-at-rest backup file guarantees by itself.
  Previously this guide claimed a non-empty target would simply fail with a
  MySQL error on its own; that claim was wrong (`mysqldump` enables
  `--add-drop-table` by default, so an old-format dump restoring over a
  same-named table would have silently replaced it, not failed) and has
  been corrected here -- the explicit check described above is the real
  protection.
- **The empty-target check is a check, not a lock (time-of-check/time-of-use).**
  See the limitation noted under "Restore procedure" above.
- **The live MySQL drill evidence below (2026-11-01) predates this
  correction.** It demonstrates the checksum, encryption and row-count
  machinery against a real server, but it restored into a target that
  happened to be empty -- it never exercised the empty-target *check*
  itself (that logic did not exist yet). The check is covered by mocked
  tests (`test_restore_refuses_non_empty_target_and_never_invokes_mysql` and
  related); it has not yet been re-run as part of a fresh live drill.
- **No disaster-recovery runbook yet** (which scenario gets which response,
  RPO/RTO targets) -- tracked as item P5.
- **Installation identity (O1).** A backup carries the `organizations`,
  `branches` and `installation` tables like any other, and a restore brings
  back the exact same UUIDs (`tests/test_installation_backup_restore.py`).
  Restoring one branch's backup into another branch's server would give that
  server the first branch's identity -- restore a backup only onto the branch
  it came from. Check with `flask installation show` after a restore.

### Evidence: the 2026-11-01 disposable-MySQL drill

Run against a real MySQL 8.0.46 server, entirely inside disposable
`*_test` databases (`quick_junction_test` as the source,
`quick_junction_restore_test` as a *separate* restore target -- neither the
real `quick_junction` database nor its data were touched at any point):

1. Applied the full migration chain to `quick_junction_test` (head
   `c2d5f8a1b3e6`, 19 tables) and populated it with representative data:
   `scripts/seed_demo.py`'s usual 3 categories, 18 menu items, 3 accounts,
   6 tables, plus one real order placed through `app.services.orders.checkout`
   (so `orders` and `order_items` were populated through the actual
   application code, not hand-inserted).
2. Recorded a baseline: 19 tables, their row counts, and the exact contents
   of the `users`, `orders`, `order_items` and a `menu_items` sample.
3. Ran `backup_database.py` against it: produced a 53,412-byte encrypted
   backup from a 39,985-byte raw dump, checksum
   `5b5fef1b9b879653...`, metadata recording all 19 tables' row counts
   matching the baseline exactly.
4. Independently recomputed the SHA-256 of the backup file outside the
   tool -- matched the recorded checksum. Confirmed the file's bytes are
   genuinely Fernet ciphertext, not readable SQL.
5. **Corrupted-backup rejection, for real**: copied the backup, flipped a
   few bytes in the middle, and ran a restore against it -- refused at the
   checksum-verification step with a clear error, before any database
   connection was attempted.
6. **Restore-failure handling, for real**: attempted a restore into a
   `_test`-suffixed database that does not exist -- refused with a clear
   "cannot connect, this tool never creates one" error. Attempted a restore
   into the real `quick_junction` name without `--allow-production-target`
   -- refused immediately, before even checking the backup file, with the
   exact confirmation phrase required printed back.
7. Restored the verified backup into the **separate** `quick_junction_restore_test`
   database. Result: all 19 tables present with names matching the source
   exactly; every table's row count matched the metadata recorded at backup
   time (reported per-table by the tool); independently re-queried the
   restored `users`, `orders`, `order_items` and `menu_items` sample rows
   and compared them directly against the baseline -- byte-for-byte
   identical, including the order's snapshotted price (`Rs. 562.74` total,
   `Rs. 249.00` unit price, `"Paneer Tikka"` name snapshot).
8. Confirmed afterward that `quick_junction_test` (the source) still had
   exactly its original data, and that the real `quick_junction` database's
   row counts were unaffected by any of the above.

`tests/test_backup_restore.py` has 40 tests: 39 run with no MySQL server at
all (the `mysqldump`/`mysql` subprocess calls, and `pymysql.connect` for the
empty-target and row-count checks, are mocked, so metadata, checksum,
encryption, retention, the empty-target gate, timeout handling, and every
other safety gate above are exercised in isolation), plus
`test_live_backup_and_restore_drill`, which repeats steps 1-3 and 7 above
automatically whenever `TEST_DATABASE_URL` points at a disposable `*_test`
MySQL database -- the same convention `tests/test_mysql_concurrency.py`
already uses. The 12 tests added in the 2026-11-02 correction pass cover:
`--skip-add-drop-table` being present in the `mysqldump` command;
`subprocess.TimeoutExpired` from either `mysqldump` or `mysql` becoming a
clear `BackupError` instead of a raw traceback; a non-empty target being
refused (proving the `mysql` restore subprocess is never invoked) for both
a `_test`-suffixed and a fully-confirmed production-named target; an empty
target being allowed to proceed; a post-restore row-count check that hits a
MySQL error being wrapped in a clear `BackupError` with the original
exception preserved as its cause; and the MySQL option-file password
quoting correctly handling `#`, quotes and backslashes while rejecting a
newline, carriage return or NUL byte outright.

---

## 20. Development vs production

This guide creates a **development / local** installation: `APP_ENV=development`, the
Flask development server (`run.py`), plain HTTP on `127.0.0.1`, optional demo accounts
with a public password.

It is **not** a production deployment. The repository has no production deployment
configuration (no WSGI service, TLS or reverse proxy — project issue #28). A real
deployment would need, at least: `APP_ENV=production`, a WSGI server
(`gunicorn "app:create_app('production')"` is the documented entry point), HTTPS,
separate migration and runtime database accounts, no demo data, and backups.

---

### Resetting the database (destructive)

`scripts\reset_database.bat` deletes **everything** in `quick_junction` and rebuilds
it from the migrations. It only runs after you type exactly `RESET QUICK JUNCTION`,
touches no other database, and is never part of normal setup. Back up first
(section 19).
