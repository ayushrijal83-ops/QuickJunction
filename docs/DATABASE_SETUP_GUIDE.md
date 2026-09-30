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
   is hidden) — used only to create the database and account, never saved;
4. creates the database `quick_junction` (utf8mb4) **if it does not exist** — an
   existing database is kept, nothing is dropped;
5. creates `'qj_user'@'127.0.0.1'` and `'qj_user'@'localhost'` with privileges on
   `quick_junction` only;
6. writes `DATABASE_URL` and `SECRET_KEY` to `.env` (the password is never shown);
7. runs every migration, verifies the result, starts the application once as a smoke
   test, and offers to load demo data.

It never starts the application for you, and it is **safe to run again**: the second
time it finds everything in place, applies 0 migrations and leaves `.env` unchanged.

If `.venv` already exists, **`scripts\setup_database.bat`** runs only the database part.

**What the script runs (for learning — you do not need to type this).** Inside
`mysql -u root -p`:

```sql
CREATE DATABASE IF NOT EXISTS quick_junction
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'qj_user'@'127.0.0.1' IDENTIFIED BY 'YOUR_PASSWORD';
CREATE USER 'qj_user'@'localhost' IDENTIFIED BY 'YOUR_PASSWORD';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON quick_junction.* TO 'qj_user'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON quick_junction.* TO 'qj_user'@'localhost';
```

Why these privileges: `SELECT/INSERT/UPDATE/DELETE` for the running application, and
`CREATE/ALTER/DROP/INDEX/REFERENCES` because the same account runs the migrations
(`flask db upgrade` creates tables, indexes, foreign keys and CHECK constraints).
Nothing outside `quick_junction`, and no server-wide rights such as `CREATE USER`,
`GRANT` or `FILE`. Both host forms exist because MySQL treats `127.0.0.1` and
`localhost` as different accounts.

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

The setup script runs this for you. To run it yourself (PowerShell, project folder):

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

- `qj_user` can log in to `quick_junction`;
- the database revision equals the repository head;
- all 14 application tables exist, InnoDB, utf8mb4;
- foreign keys and CHECK constraints exist, and the `pricing_settings` row is there;
- the application starts and `/health` and `/menu` answer 200 using the database.

To look for yourself, inside `mysql -u root -p`:

```sql
SHOW DATABASES;
USE quick_junction;
SHOW TABLES;
SELECT version_num FROM alembic_version;
SHOW GRANTS FOR 'qj_user'@'127.0.0.1';
```

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

**Getting the first administrator — two ways:**

1. **Demo data (development only).** Answer **Y** to "Load demo data now?" during
   setup, or run later:

   ```powershell
   .\.venv\Scripts\python scripts\seed_demo.py
   ```

   It adds a sample menu, six tables and three demo accounts — `customer`, `staff`,
   `admin` — all with the password **`demo-password-1`**. That password is public (it
   is in the repository), so use these accounts only on your own computer. The script
   refuses to run in a production configuration and is safe to run more than once.

2. **Promote your own account.** Register a normal customer account at `/register`,
   then, inside `mysql -u root -p`:

   ```sql
   UPDATE quick_junction.users SET role = 'admin' WHERE username = 'your_username';
   ```

   Sign in at `/login/admin`. (Roles are stored as `admin`, `staff`, `customer`.)

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

Backups are **not** made automatically by the setup scripts. Before a reset, an
upgrade or moving computers, make one. In PowerShell (it asks for the root password):

```powershell
mysqldump -u root -p --single-transaction --routines --default-character-set=utf8mb4 quick_junction > D:\qj_backup.sql
```

Restore into an **existing, empty** `quick_junction` database:

```powershell
cmd /c "mysql -u root -p --default-character-set=utf8mb4 quick_junction < D:\qj_backup.sql"
```

(`cmd /c` is used because PowerShell's `<` does not work for input redirection.)
Keep backup files out of the project folder and out of Git — they contain accounts,
orders and audit data.

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
