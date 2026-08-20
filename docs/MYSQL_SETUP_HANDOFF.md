# Quick Junction — MySQL Database Setup & Handover Guide

**Audience:** a developer who has just received the Quick Junction project and has a
completely fresh Windows machine.

**Assumed knowledge:** none. You do not need to know Flask, SQLAlchemy, Alembic, or
anything about how Quick Junction is built. Every term is explained the first time it
appears.

**What you get at the end:** a working Quick Junction installation, backed by a real
MySQL database, that you can open in a browser and log into.

> **Every statement in this document was checked against the source code in this
> repository**, not copied from other documentation. Where a value could be confused
> with a real secret, a placeholder is used instead. **No real password appears
> anywhere in this file.**

---

## Table of contents

| § | Section |
| --- | --- |
| 1 | [What this document does](#1--what-this-document-does) |
| 2 | [Requirements on the new computer](#2--requirements-on-the-new-computer) |
| 3 | [Install MySQL](#3--install-mysql) |
| 4 | [Create the Quick Junction database](#4--create-the-quick-junction-database) |
| 5 | [Create the application database user](#5--create-the-application-database-user) |
| 6 | [Configure `.env`](#6--configure-env) |
| 7 | [Create the Python virtual environment](#7--create-the-python-virtual-environment) |
| 8 | [Install dependencies](#8--install-dependencies) |
| 9 | [Database migrations](#9--database-migrations) |
| 10 | [Database tables](#10--database-tables) |
| 11 | [Seed demo data](#11--seed-demo-data) |
| 12 | [Verify the MySQL connection](#12--verify-the-mysql-connection) |
| 13 | [Start Quick Junction](#13--start-quick-junction) |
| 14 | [First-time setup checklist](#14--first-time-setup-checklist) |
| 15 | [New machine verification](#15--new-machine-verification) |
| 16 | [Backup and recovery](#16--backup-and-recovery) |
| 17 | [Troubleshooting](#17--troubleshooting) |

---

## 1 — What this document does

### 1.1 Why Quick Junction requires MySQL

Quick Junction is a restaurant ordering system. It handles **money** and **orders**,
and both of those impose requirements that a toy database cannot meet:

* **Prices must be exact.** Every price column in this project is
  `Numeric(10, 2)` — a fixed-point decimal with exactly two decimal places — and is
  mapped to Python's `Decimal` type, never to a floating-point number. You can see
  this in `app/models/menu_item.py`:

  ```python
  price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)
  ```

  Floating-point arithmetic cannot represent most decimal fractions exactly, which is
  how a total ends up one paisa out. MySQL's `DECIMAL` type stores the number exactly.

* **Relationships must be enforced by the database, not just by the application.**
  An `order_items` row points at an `orders` row and at a `menu_items` row. If the
  application had a bug, the database itself must still refuse to create an order line
  that points at nothing. MySQL's InnoDB storage engine enforces foreign keys by
  default.

* **Rules must survive careless writes.** The project uses `CHECK` constraints so
  that even a direct `INSERT` typed by hand into a SQL console cannot create a menu
  item priced at zero, or an order with a negative total.

### 1.2 What MySQL stores

MySQL holds **all persistent Quick Junction data**:

| Data | Table |
| --- | --- |
| User accounts, roles, password hashes | `users` |
| Menu categories | `categories` |
| Menu items and their prices | `menu_items` |
| Ingredients | `ingredients` |
| Which ingredient belongs to which item | `menu_item_ingredients` |
| Customer taste preferences | `customer_preferences` |
| Orders | `orders` |
| Order lines with frozen price snapshots | `order_items` |
| Security audit trail | `audit_logs` |
| Which migrations have been applied | `alembic_version` |

**What MySQL does *not* store:**

* **The shopping cart.** This surprises people, so it is worth stating plainly:
  **there is no cart table in Quick Junction.** The cart lives inside the browser's
  signed session cookie as a plain `{"<menu_item_id>": quantity}` dictionary. See
  `app/utils/cart.py`. Only an item id and a quantity are ever stored — never a
  price, a name, or a total. The prices you see on the cart page are re-read from
  `menu_items` on every single request.
* **The AI model weights.** Those are 1.4 GB of files under `models/`, transferred
  separately (see §2.4).
* **Application logs.** Those go to files under `logs/`.

### 1.3 Why SQLite is not the production database

SQLite is used by this project — but **only for running the automated tests**. You can
see this in `config.py`:

```python
class TestingConfig(BaseConfig):
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
```

`sqlite://` with nothing after it means "an in-memory database that vanishes when the
process exits". That is ideal for tests: 420 tests run in about 30 seconds and touch
no real data.

It is *not* suitable for running the product, and the project actively prevents it.
`ProductionConfig.validate()` in `config.py` refuses to start:

```python
uri = cls.SQLALCHEMY_DATABASE_URI or ""
if uri.startswith("sqlite"):
    raise ConfigError("Production requires the MySQL DATABASE_URL.")
```

There is also a subtler reason. SQLite ignores foreign-key constraints unless a
connection explicitly turns them on. The project works around this for tests in
`app/extensions.py`:

```python
if type(dbapi_connection).__module__.startswith("sqlite3"):
    cursor.execute("PRAGMA foreign_keys=ON")
```

That comment in the source says it best — without this, the test suite "would pass
tests that a real MySQL deployment could fail differently on". MySQL needs no such
workaround.

### 1.4 The five things you are about to install, and how they relate

These names get used interchangeably in casual conversation, which causes real
confusion. They are five distinct things:

| Thing | What it actually is |
| --- | --- |
| **MySQL Server** | A background program (a Windows *service*) that runs continuously and listens on a network port, by default **3306**. It can hold many unrelated databases at once. |
| **MySQL database / schema** | A named container of tables inside that server. Quick Junction uses one, named **`quick_junction`**. In MySQL, "database" and "schema" mean the same thing. |
| **MySQL user** | A login account *inside* MySQL — completely separate from your Windows account. It has a name, a password, and a list of things it is allowed to do. |
| **SQLAlchemy** | A Python library. It lets Python code describe tables as Python classes and turns Python method calls into SQL. Quick Junction's tables are defined in `app/models/`. |
| **Flask-Migrate / Alembic** | Alembic is the tool that applies versioned changes to the database structure. Flask-Migrate is a thin wrapper that plugs Alembic into Flask so you can type `flask db upgrade`. The version files live in `migrations/versions/`. |

### 1.5 Conceptual diagram

```text
┌─────────────────────────────────────────────────────────────┐
│ Windows                                                     │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ MySQL Server  (a Windows service, listening on 3306)  │  │
│  │                                                       │  │
│  │   ┌───────────────────────────────────────────────┐   │  │
│  │   │ Database:  quick_junction                     │   │  │
│  │   │   users, categories, menu_items,              │   │  │
│  │   │   ingredients, menu_item_ingredients,         │   │  │
│  │   │   customer_preferences, orders, order_items,  │   │  │
│  │   │   audit_logs, alembic_version                 │   │  │
│  │   └───────────────────────────────────────────────┘   │  │
│  └───────────────────────────▲───────────────────────────┘  │
│                              │ SQL over TCP, port 3306      │
│                              │ authenticated as qj_user     │
│  ┌───────────────────────────┴───────────────────────────┐  │
│  │ SQLAlchemy  (+ PyMySQL driver)                        │  │
│  │   Turns Python objects into SQL and back again.       │  │
│  └───────────────────────────▲───────────────────────────┘  │
│                              │                              │
│  ┌───────────────────────────┴───────────────────────────┐  │
│  │ Flask application  (python run.py)                    │  │
│  │   Routes → Services → Models. Renders HTML.           │  │
│  │   Listening on 127.0.0.1:5000                         │  │
│  └───────────────────────────▲───────────────────────────┘  │
└──────────────────────────────┼──────────────────────────────┘
                               │ HTTP
                    ┌──────────┴──────────┐
                    │ Browser             │
                    │ http://127.0.0.1:5000 │
                    └─────────────────────┘
```

**Reading it from the bottom up:** you open a page in the browser. The browser sends
an HTTP request to the Flask application. Flask decides which Python function handles
it. That function calls a *service* function, which uses SQLAlchemy to build a SQL
query. SQLAlchemy sends that SQL to MySQL over TCP port 3306, logged in as the
`qj_user` account. MySQL returns rows. SQLAlchemy turns them back into Python objects.
Flask renders them into HTML. The browser displays the page.

---

## 2 — Requirements on the new computer

Everything below was taken from `HANDOFF.md`, `README.md`, `requirements.txt`,
`requirements-ai.txt` and the actual working installation.

### 2.1 Required for the normal application

| Requirement | Version | Notes |
| --- | --- | --- |
| **Windows** | Windows 10 or 11 | The project was developed and verified on Windows 11. **Nothing in the code is Windows-specific** — `HANDOFF.md` states Linux is equally supported. This guide gives Windows commands because that is what you asked for. |
| **Python** | **3.10 or newer** | Developed and verified on **3.10.11**. The code uses `X | None` type syntax, which requires 3.10 as a hard minimum. |
| **MySQL** | **8.0 or newer** | Verified against **8.0.46**. |
| **Git** | any recent version | Only needed to clone the repository. If someone hands you a ZIP instead, you do not need Git at all. |
| **Internet** | during setup only | Needed once, to download Python packages from PyPI. After installation the application never makes an outbound network call — see §2.5. |
| **Disk** | ~500 MB | Python packages for the web application, plus the database. |
| **RAM** | ~1 GB free | The web application alone is small. |

**Not required:** a Visual C++ build toolchain. Every dependency in
`requirements.txt` ships as a pre-built wheel for Windows. The MySQL driver is
**PyMySQL**, which is pure Python precisely so that no C compiler is needed — the
comment in `requirements.txt` says so directly:

```text
# MySQL driver. Pure Python, so no C toolchain is needed on Windows.
PyMySQL==1.2.0
```

**Not required:** a GPU. Nowhere, for anything.

### 2.2 Required only for local AI explanations

The AI explanation feature is **optional**. If you skip everything in this subsection,
Quick Junction still runs completely — you simply see the message *"AI explanation
temporarily unavailable"* on one panel of one page, and every other feature works
normally.

| Requirement | Detail |
| --- | --- |
| **`requirements-ai.txt` installed** | PyTorch, Transformers, PEFT, Accelerate, SafeTensors, Tokenizers, huggingface_hub. |
| **Extra disk** | About **1.5 GB** for the model weights, plus roughly **2–3 GB** for PyTorch and its dependencies. |
| **Extra RAM** | The base model is 1.19 GB of float32 weights and is held resident in memory once loaded. Budget **at least 4 GB free RAM**, comfortably more. |
| **Model files** | The `models/` directory. **It is not in Git** and cloning does not fetch it. It must be copied to you by the project owner — see §2.4. |
| **CPU only** | Explicitly. `HANDOFF.md`: *"GPU — **Not required.** Everything is CPU-only by design."* |

### 2.3 Deciding which path to take

```text
Do you need the AI explanation panel to work?
        │
   ┌────┴────┐
   NO        YES
   │          │
   │          ├─ install requirements.txt
   │          ├─ install requirements-ai.txt
   │          └─ obtain and place models/
   │
   └─ install requirements.txt only
      Everything works except one panel.
```

### 2.4 The `models/` directory

```text
models/
├── Qwen3-0.6B-Base/                     ~1.2 GB   REQUIRED for AI
└── qwen3-0.6b-quickjunction-lora-v4/    ~35 MB    REQUIRED for AI (production adapter)
```

Three older adapter directories (`qwen3-0.6b-quickjunction-lora`, `-v2`, `-v3`) may
also be present on the machine you receive. They are **historical artifacts kept for
reproducibility and are not needed to run the application**. See
`docs/TECHNICAL_HANDOFF.md` §19.

`models/` is listed in `.gitignore` (line 64, as `/models/`), so it is deliberately
excluded from version control. There is no download URL documented in this repository
and none should be assumed. **The directory must be transferred to you separately by
the project owner** — a USB drive, a network share, or a file-transfer service.

### 2.5 A note on internet access

Once the packages are installed, Quick Junction **never contacts the internet**:

* The AI model runs locally. `app/services/local_llm.py` sets
  `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` *before* the `transformers` library
  is imported, and loads weights with `local_files_only=True`. There is no API key
  anywhere in the project because there is no external service to call.
* Bootstrap (the CSS framework) is **vendored locally** under
  `app/static/vendor/`, not loaded from a CDN. The comment in
  `app/templates/base.html` explains why: *"this project is offline-first ... a CDN
  would add a runtime dependency on the public internet that nothing else here has."*

---

## 3 — Install MySQL

### 3.1 Where to get it

Download the **MySQL Installer for Windows** from the official Oracle download page:

```text
https://dev.mysql.com/downloads/installer/
```

Choose the installer for **MySQL Community Server 8.0 or newer**. The project is
verified against 8.0.46. The smaller "web" installer downloads components as it goes;
the larger offline installer contains everything.

### 3.2 Which components you need

The installer offers a long list. You need:

| Component | Needed? | Why |
| --- | --- | --- |
| **MySQL Server** | **Yes** | This is the database itself. Nothing works without it. |
| **MySQL Workbench** | Recommended | A graphical tool for running SQL and browsing tables. Optional but makes §4 and §16 much easier. |
| **MySQL Shell** | Optional | A modern command-line client. The classic `mysql` command-line client is enough. |
| Connector/ODBC, Connector/J, Connector/NET | **No** | Those are drivers for other languages. Python uses PyMySQL, which pip installs. |
| MySQL for Visual Studio | **No** | Not used by this project. |
| MySQL Router | **No** | For clustered deployments. Not used. |
| Sample databases | **No** | Not used. |

If the installer offers a "Setup Type" choice, **Custom** and picking Server +
Workbench is the leanest option. "Developer Default" also works and simply installs
more than you need.

### 3.3 Installing MySQL Server

1. Run the installer and select the components above.
2. When it reaches **Type and Networking**:
   * Config type: **Development Computer** (uses the least memory).
   * **TCP/IP: enabled**, **Port: 3306**. This matters — Quick Junction connects over
     TCP, not over a named pipe. Leave the port at 3306 unless you have a reason to
     change it; if you do change it, you must reflect that in `.env` (§6).
3. When it reaches **Authentication Method**:
   * Choose **"Use Strong Password Encryption"** (this is `caching_sha2_password`,
     the MySQL 8 default). PyMySQL 1.2.0 supports it.
   * If you hit an authentication-plugin error later, §17 covers the fallback.
4. When it reaches **Accounts and Roles**:
   * Set the **root password**. Throughout this document that account is referred to
     as `MYSQL_ADMIN_USER` (its name is normally `root`) with password
     `MYSQL_ADMIN_PASSWORD`.

   > **You choose this password. It is not supplied by this project and it is not
   > written down anywhere in this repository.** Pick something strong, store it in a
   > password manager, and do not put it in `.env` — `.env` holds the *application*
   > user's password, which is a different account (§5).

5. When it reaches **Windows Service**:
   * Leave **"Configure MySQL Server as a Windows Service"** ticked and
     **"Start the MySQL Server at System Startup"** ticked. This means MySQL comes
     back automatically after a reboot, and you never have to remember to start it.
   * Note the service name it offers, typically **`MySQL80`**.
6. Click **Execute** to apply the configuration, then **Finish**.

### 3.4 Verify the MySQL service is running

Open PowerShell and run:

```powershell
Get-Service -Name "MySQL*"
```

Expected output — the important column is `Status`:

```text
Status   Name               DisplayName
------   ----               -----------
Running  MySQL80            MySQL80
```

If it says `Stopped`, start it (this needs an Administrator PowerShell):

```powershell
Start-Service -Name "MySQL80"
```

You can also check that something is actually listening on the port:

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 3306
```

`TcpTestSucceeded : True` means MySQL is reachable.

### 3.5 Open the MySQL command line

The `mysql` client is not on your `PATH` by default. Either add it, or call it by full
path. The usual location is:

```text
C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe
```

To connect as the administrator:

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u MYSQL_ADMIN_USER -p
```

Replace `MYSQL_ADMIN_USER` with the actual admin account name (normally `root`). The
`-p` flag makes MySQL prompt you for the password rather than taking it on the command
line — **always use `-p` this way**; a password typed as `-pSecret` is stored in your
PowerShell history in plain text.

You should land at:

```text
mysql>
```

### 3.6 Test it

At the `mysql>` prompt:

```sql
SELECT VERSION();
```

Expected shape of the output:

```text
+-----------+
| VERSION() |
+-----------+
| 8.0.46    |
+-----------+
```

**Any 8.0 or newer version is acceptable.** The reference installation for this
project reports `8.0.46`. If you see a 5.7 version, stop and upgrade — this project
has not been tested against MySQL 5.x and uses `CHECK` constraints, which MySQL 5.7
silently ignores.

Type `exit;` to leave the client.

---

## 4 — Create the Quick Junction database

### 4.1 What the project actually expects

Two sources in the repository agree on the database name:

* `.env.example`:
  ```ini
  DATABASE_URL=mysql+pymysql://qj_user:CHANGE_ME@127.0.0.1:3306/quick_junction
  ```
* `HANDOFF.md`, in its MySQL setup section, creates `quick_junction` with
  `utf8mb4` / `utf8mb4_unicode_ci`.

So the values are:

| Setting | Value |
| --- | --- |
| Database name | **`quick_junction`** |
| Character set | **`utf8mb4`** |
| Collation | **`utf8mb4_unicode_ci`** |

You *can* choose a different database name — nothing in the Python code hard-codes it,
because the name only appears in the `DATABASE_URL` you write into `.env`. But if you
change it, you must change it in `.env` too, and every other document in this
repository will disagree with you. **Use `quick_junction` unless you have a specific
reason not to.**

### 4.2 Why `utf8mb4` matters

MySQL has a confusing historical wart: a character set literally called `utf8`, which
is **not** real UTF-8. It only stores characters up to three bytes, which covers most
of the world's writing systems but **excludes anything in the four-byte range** —
emoji, some rarer CJK characters, some historic scripts.

`utf8mb4` ("UTF-8, multi-byte 4") is the real, complete UTF-8.

This is not theoretical for Quick Junction. The application renders prices with the
Indian rupee sign **₹** (`app/utils/formatting.py` defines
`CURRENCY_SYMBOL = "₹"`), and menu items, descriptions and ingredient names are free
text that a restaurant owner may well write in a non-Latin script. Choosing the wrong
character set produces `Incorrect string value` errors on insert, or silently mangled
text.

`utf8mb4_unicode_ci` is the *collation* — the rule for comparing and sorting strings.
The `_ci` suffix means **case-insensitive**, so `'Starters'` and `'starters'` are
treated as the same value for uniqueness and ordering.

> **Related detail worth knowing:** because MySQL's default collation is
> case-insensitive but SQLite's is not, the application normalises usernames and
> emails to lowercase *in Python* before storing them
> (`app/utils/security.py::normalize_username`). The comment there says this is
> deliberately done "so uniqueness does not depend on the database's collation". You
> do not need to do anything about this — it is already handled.

### 4.3 Option A — MySQL command line

Connect as the administrator (§3.5), then:

```sql
CREATE DATABASE quick_junction
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

Verify it exists:

```sql
SHOW DATABASES LIKE 'quick_junction';
```

Verify the character set and collation actually took:

```sql
SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME
FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME = 'quick_junction';
```

Expected:

```text
+----------------------------+------------------------+
| DEFAULT_CHARACTER_SET_NAME | DEFAULT_COLLATION_NAME |
+----------------------------+------------------------+
| utf8mb4                    | utf8mb4_unicode_ci     |
+----------------------------+------------------------+
```

### 4.4 Option B — MySQL Workbench

1. Open MySQL Workbench and click your local connection (it will be pre-created by the
   installer). Enter the admin password when prompted.
2. Click the **Create a new schema** button in the toolbar (a cylinder with a `+`), or
   open a query tab and paste the `CREATE DATABASE` statement from §4.3.
3. If using the graphical dialog:
   * **Name:** `quick_junction`
   * **Charset:** `utf8mb4`
   * **Collation:** `utf8mb4_unicode_ci`
4. Click **Apply**, review the SQL Workbench generated, and **Apply** again.
5. The new schema appears in the left-hand **Schemas** panel. It will be empty — that
   is correct. Tables are created by migrations in §9, not by you.

### 4.5 What if the database already exists?

`CREATE DATABASE` fails with:

```text
ERROR 1007 (HY000): Can't create database 'quick_junction'; database exists
```

That is not a problem in itself. Decide which case you are in:

| Situation | What to do |
| --- | --- |
| You already created it a minute ago and got interrupted | Nothing. Move to §5. |
| Someone restored a backup into it | Nothing. Skip the seeding step in §11 and run `flask --app run.py db upgrade` (§9) to bring the structure up to date. |
| It exists but you do not know what is in it | Look before you act: `USE quick_junction; SHOW TABLES;` |
| You are certain it is junk and want to start over | See the warning below. |

To make `CREATE DATABASE` silently succeed when the database is already there, use:

```sql
CREATE DATABASE IF NOT EXISTS quick_junction
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

> **⚠ `DROP DATABASE quick_junction;` permanently destroys every user account, every
> order and the entire audit trail. There is no undo.** Only run it if you are
> certain the data is disposable, and take a backup first (§16).

---

## 5 — Create the application database user

### 5.1 The principle

> **Do not run the Flask application as MySQL `root`.**

This is not bureaucracy. `root` can drop *any* database on the server, read any
table, and create new administrative accounts. If the application had a SQL-injection
bug, or if someone got hold of the `.env` file, the damage would be bounded by
whatever the connecting account is allowed to do. Give it exactly the privileges it
needs on exactly one database, and nothing more.

This is the **principle of least privilege**, and `HANDOFF.md` already applies it —
the DSN it documents connects as `qj_user`, not as `root`.

### 5.2 The account this project expects

From `.env.example`:

```ini
DATABASE_URL=mysql+pymysql://qj_user:CHANGE_ME@127.0.0.1:3306/quick_junction
```

| Setting | Value |
| --- | --- |
| Username | **`qj_user`** |
| Host | **`127.0.0.1`** |
| Port | **3306** |
| Database | **`quick_junction`** |
| Password | **you choose it** — referred to below as `CHOOSE_A_STRONG_PASSWORD` |

### 5.3 `127.0.0.1` and `localhost` are not the same thing to MySQL

This trips people up often enough to deserve its own subsection.

In MySQL, a user account is identified by **the pair** `'name'@'host'`.
`'qj_user'@'localhost'` and `'qj_user'@'127.0.0.1'` are **two different accounts**,
and creating one does not create the other.

Worse, MySQL treats the two host strings differently at connection time: `localhost`
traditionally means "connect over a local socket or named pipe", while `127.0.0.1`
means "connect over TCP to the loopback address".

**Quick Junction's DSN uses `127.0.0.1`.** PyMySQL will therefore connect over TCP and
MySQL will match it against the `'qj_user'@'127.0.0.1'` account. If you only create
`'qj_user'@'localhost'`, you will get `Access denied` and spend a frustrating hour on
it.

The safe move is to create both, or to use the wildcard `'qj_user'@'%'` — but `%`
means "from any host on the network", which is a wider grant than a development
machine needs. Creating both explicit accounts is better.

### 5.4 Create the user and grant privileges

Connect as the administrator (§3.5), then run:

```sql
-- The account the DSN in .env will actually use.
CREATE USER 'qj_user'@'127.0.0.1'
  IDENTIFIED BY 'CHOOSE_A_STRONG_PASSWORD';

-- Also create the localhost form, so a client that resolves to a socket works too.
CREATE USER 'qj_user'@'localhost'
  IDENTIFIED BY 'CHOOSE_A_STRONG_PASSWORD';

-- Privileges on the Quick Junction database only. Note `quick_junction.*` --
-- the `.*` means "every table in this one database", not "every database".
GRANT ALL PRIVILEGES ON quick_junction.* TO 'qj_user'@'127.0.0.1';
GRANT ALL PRIVILEGES ON quick_junction.* TO 'qj_user'@'localhost';

FLUSH PRIVILEGES;
```

> **`CHOOSE_A_STRONG_PASSWORD` is a placeholder.** Replace it with a password you
> generate. Use the same value in both `CREATE USER` statements and in `.env` (§6).
> **Do not write the real value into any file that is tracked by Git.**

Verify the grants landed:

```sql
SHOW GRANTS FOR 'qj_user'@'127.0.0.1';
```

You should see a `USAGE` line (which means "can log in") and a line granting
privileges `ON \`quick_junction\`.*`. If you see `ON *.*`, you granted server-wide
privileges by mistake — revoke and redo.

### 5.5 Why these privileges, and why not more

**Why the application user needs privileges on the database:**

| Operation | Needed by |
| --- | --- |
| `SELECT` | Every page. Reading the menu, orders, users. |
| `INSERT` | Registration, checkout, adding menu items, every audit-log row. |
| `UPDATE` | Saving preferences, changing an order's status, `session_version` on logout. |
| `DELETE` | Removing an ingredient association when a menu item's ingredient list changes (`sync_menu_item_ingredients`). |
| `CREATE`, `ALTER`, `DROP`, `INDEX`, `REFERENCES` | **Migrations.** `flask db upgrade` creates and alters tables. If you intend to run migrations as `qj_user`, it needs these. |

`GRANT ALL PRIVILEGES ON quick_junction.*` covers all of the above, scoped to one
database. That is the pragmatic setting for a development or demo machine.

**Why it does not need global administrative privileges:**

`CREATE DATABASE`, `CREATE USER`, `GRANT`, `SHUTDOWN`, `FILE`, `PROCESS` and
`SUPER` are all server-level operations. The application performs none of them. It
connects to a database that already exists and works inside it. Creating a database is
an operator task you did once, by hand, as the admin — which is exactly why §4 is a
separate section from §5.

**A stricter variant, if you want it.** For a real deployment where migrations are run
separately by an operator, the runtime account can be narrowed to just the data
operations:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON quick_junction.* TO 'qj_user'@'127.0.0.1';
```

`docs/SECURITY.md` §17 lists *"A dedicated MySQL user with least privilege — no
`GRANT ALL` in production"* as work still outstanding, so this is a known and already
documented gap rather than something you have discovered.

---

## 6 — Configure `.env`

### 6.1 What `.env` is and why it is not in Git

`.env` is a plain text file of `NAME=value` lines that sits in the project root. When
the application starts, `config.py` reads it:

```python
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
```

It is **local machine configuration**. It contains the database password and the
application's signing key — the two most sensitive values in the project.

`.env` is listed in `.gitignore` (line 4). **It must stay that way.** If a real
`SECRET_KEY` or database password is ever committed, treat it as compromised: rotate
the value first, clean history second.

`.env.example` is the **safe template**. It is committed, contains only placeholders,
and exists so a new developer can see which variable names are expected without
anyone having to share a secret.

Create your `.env` by copying the template:

```powershell
Copy-Item .env.example .env
```

Then open `.env` in an editor and fill in the two values that matter.

### 6.2 Every variable Quick Junction actually reads

Taken from `config.py` and `.env.example`. Nothing else in the file is read by the
application.

| Variable | Purpose | Example (placeholder) | Required? |
| --- | --- | --- | --- |
| `APP_ENV` | Which configuration class to load: `development`, `testing`, or `production`. Defaults to `development` if unset. | `development` | No — defaults to `development` |
| `SECRET_KEY` | Signs the session cookie (and therefore the cart and the login state). | `<64+ random characters>` | **Yes** — startup fails without it |
| `DATABASE_URL` | The full MySQL connection string. | `mysql+pymysql://qj_user:PASSWORD@127.0.0.1:3306/quick_junction` | **Yes** — startup fails without it |
| `TEST_DATABASE_URL` | Overrides the in-memory SQLite database used by pytest. | *(leave commented out)* | No |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. | `DEBUG` | No — defaults to `INFO` (`DEBUG` in development) |
| `FLASK_RUN_HOST` | Address `run.py` binds to. | `127.0.0.1` | No — defaults to `127.0.0.1` |
| `FLASK_RUN_PORT` | Port `run.py` binds to. | `5000` | No — defaults to `5000` |
| `LLM_ENABLED` | Master switch for AI explanations. | `true` | No — defaults to `true` |
| `LLM_MODEL_PATH` | Directory of the base model weights. | `models/Qwen3-0.6B-Base` | No — built-in default is correct |
| `LLM_ADAPTER_PATH` | Directory of the production LoRA adapter. | `models/qwen3-0.6b-quickjunction-lora-v4` | No — built-in default is correct |
| `LLM_MAX_NEW_TOKENS` | Caps how long a generated explanation can be, which caps how long it takes. | `48` | No — defaults to `48` |
| `LLM_WARMUP` | Load the model in a background thread at startup instead of on first use. | `true` | No — defaults to **on** in development, **off** in production |

> **Leave every `LLM_*` variable commented out** unless your `models/` directory is
> somewhere non-standard. The built-in defaults in `config.py` are absolute paths
> derived from the repository's own location, so they are already correct and cannot
> break when you start the app from a different working directory.
>
> `.env.example` warns about this explicitly: a *relative* path in `LLM_MODEL_PATH` is
> resolved against the current working directory, not the repository root. If you must
> set them, use absolute paths.

### 6.3 Generating `SECRET_KEY`

This value signs the session cookie. If it is weak or predictable, an attacker can
forge a session and log in as anybody.

Generate one (after you have created the virtual environment in §7, or with any
Python 3):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Paste the output into `.env`:

```ini
SECRET_KEY=<the-long-random-string-you-just-generated>
```

`ProductionConfig.validate()` in `config.py` enforces a floor: it refuses to start if
the key is shorter than 32 characters or appears on a placeholder deny-list that
includes `change-me`, `secret`, `dev` and the literal
`replace-with-a-64-char-random-string` that ships in `.env.example`. Generate a real
one — do not edit the placeholder by hand.

**Generate a different key for every environment.** A key shared between a laptop and
a server means a session forged on one is valid on the other.

### 6.4 Structure of the database URL

SQLAlchemy calls this a **DSN** (Data Source Name) or connection URL. Its shape is:

```text
mysql+pymysql://USERNAME:PASSWORD@HOST:PORT/DATABASE
└───┬──┘ └──┬──┘   └───┬──┘ └──┬───┘ └─┬─┘ └┬─┘ └──┬───┘
    │       │          │       │       │    │      └─ the database created in §4
    │       │          │       │       │    └──────── 3306, from §3.3
    │       │          │       │       └───────────── 127.0.0.1, matching the user in §5
    │       │          │       └───────────────────── the password you chose in §5
    │       │          └───────────────────────────── qj_user, created in §5
    │       └──────────────────────────────────────── the driver: PyMySQL
    └──────────────────────────────────────────────── the database dialect: MySQL
```

Filled in with placeholders:

```ini
DATABASE_URL=mysql+pymysql://qj_user:CHOOSE_A_STRONG_PASSWORD@127.0.0.1:3306/quick_junction
```

**`mysql+pymysql` is not optional.** Plain `mysql://` makes SQLAlchemy look for the
`mysqlclient` driver, which is not installed and would need a C compiler. The `+pymysql`
suffix tells it to use the pure-Python driver this project pins.

### 6.5 Special characters in the password — URL encoding

A DSN is a URL, so a handful of characters have structural meaning inside it and must
be **percent-encoded** if they appear in your password:

| Character | Encode as | Why it breaks things |
| --- | --- | --- |
| `@` | `%40` | Separates credentials from host — an extra one splits the URL in the wrong place |
| `:` | `%3A` | Separates username from password, and host from port |
| `/` | `%2F` | Separates host from database name |
| `#` | `%23` | Starts a URL fragment |
| `?` | `%3F` | Starts a query string |
| `%` | `%25` | Starts a percent-escape |
| space | `%20` | Ambiguous in a URL |

`.env.example` warns about this directly: *"Percent-encode any special characters in
the password."*

**Symptom of getting this wrong:** an `Access denied` error where the password looks
correct, because MySQL received a truncated version of it.

To encode a password safely, let Python do it:

```powershell
python -c "import urllib.parse, getpass; print(urllib.parse.quote(getpass.getpass('Password: '), safe=''))"
```

It prompts without echoing, and prints the encoded form to paste into `.env`.

The simplest way to avoid the whole issue is to generate a password from letters,
digits, hyphens and underscores only — none of which need encoding.

### 6.6 A worked example, with placeholders only

```ini
# Quick Junction -- local environment
APP_ENV=development

SECRET_KEY=<paste the output of secrets.token_urlsafe(64) here>

DATABASE_URL=mysql+pymysql://qj_user:CHOOSE_A_STRONG_PASSWORD@127.0.0.1:3306/quick_junction

LOG_LEVEL=DEBUG
FLASK_RUN_HOST=127.0.0.1
FLASK_RUN_PORT=5000

# Leave the LLM_* block commented out. The built-in defaults are correct.
```

---

## 7 — Create the Python virtual environment

### 7.1 Check your Python version first

```powershell
python --version
```

You need **3.10 or newer**. The reference installation is `Python 3.10.11`.

If `python` opens the Microsoft Store instead of printing a version, Python is not
installed properly — install it from [python.org](https://www.python.org/downloads/)
and tick **"Add python.exe to PATH"** during setup.

If you have several Pythons installed, the `py` launcher lets you pick:

```powershell
py -3.10 --version
```

### 7.2 Create and activate

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Your prompt should now be prefixed with `(.venv)`.

If PowerShell refuses with *"running scripts is disabled on this system"*, allow
locally-created scripts for your own account:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then run the activation command again. (In Command Prompt rather than PowerShell, the
activation script is `.venv\Scripts\activate.bat` and no policy change is needed.)

### 7.3 Verify you are inside it

```powershell
python --version
(Get-Command python).Source
```

The second command must print a path **inside your project**, ending in
`\.venv\Scripts\python.exe`. If it prints a system path, activation did not take —
check for the `(.venv)` prefix on your prompt.

### 7.4 What a virtual environment is, and why it matters here

A **virtual environment** is a private folder containing its own copy of the Python
interpreter and its own `site-packages` directory. When it is activated, `pip install`
puts packages *there* instead of into your system-wide Python.

Three reasons this matters for Quick Junction specifically:

1. **The dependencies are pinned to exact versions.** `requirements.txt` pins
   `Flask==3.1.3`, `SQLAlchemy==2.0.52`, `numpy==1.24.3` and so on. Without isolation,
   installing Quick Junction would forcibly downgrade or upgrade packages that some
   other project on your machine depends on.
2. **The AI stack is large and version-sensitive.** PyTorch and Transformers are
   several gigabytes and are pinned against each other. Mixing them into a shared
   Python is how you end up with two broken projects instead of one working one.
3. **It is disposable.** If the environment gets into a confusing state, delete
   `.venv` entirely and recreate it. Nothing of value lives there.

**Never copy the original developer's `.venv` folder.** It contains absolute paths
baked into scripts (`.venv/pyvenv.cfg` records the path of the Python that created it)
and compiled binaries built for their exact machine. Always create your own.

`.venv/` is excluded by `.gitignore` for exactly this reason.

---

## 8 — Install dependencies

### 8.1 The two files, and the difference between them

| File | Contains | Required? |
| --- | --- | --- |
| `requirements.txt` | The web application: Flask, SQLAlchemy, Alembic, PyMySQL, Flask-WTF, Argon2, scikit-learn, pytest. | **Always.** |
| `requirements-ai.txt` | Local model inference and LoRA training: PyTorch, Transformers, PEFT, Accelerate, SafeTensors, Tokenizers, huggingface_hub. | **Only if you want AI explanations.** |

The header comment in `requirements-ai.txt` states the relationship exactly:

```text
# The web application still runs without this file. If these packages are
# absent, app/services/local_llm.py logs a warning and returns None, and
# /recommendations/explain shows "AI explanation temporarily unavailable"
# while every deterministic feature keeps working.
```

One thing that looks like it belongs in the AI file but does not: **scikit-learn is in
`requirements.txt`, not `requirements-ai.txt`.** The recommendation engine uses
TF-IDF and cosine similarity from scikit-learn, and `/recommendations` is an ordinary
customer page that must work whether or not the AI stack is installed. The comment in
`requirements.txt` spells this out.

### 8.2 Install the web application (always)

With the virtual environment activated:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

The exact pinned versions, from `requirements.txt`:

| Package | Version | What it does |
| --- | --- | --- |
| `Flask` | 3.1.3 | The web framework |
| `Flask-SQLAlchemy` | 3.1.1 | Wires SQLAlchemy into Flask |
| `Flask-Migrate` | 4.1.0 | Wires Alembic into Flask (`flask db ...`) |
| `Flask-WTF` | 1.3.0 | Forms and CSRF protection |
| `SQLAlchemy` | 2.0.52 | The ORM |
| `alembic` | 1.19.1 | Database migrations |
| `PyMySQL` | 1.2.0 | Pure-Python MySQL driver |
| `python-dotenv` | 1.2.2 | Reads `.env` |
| `argon2-cffi` | 25.1.0 | Argon2id password hashing |
| `email-validator` | 2.3.0 | Backs WTForms' `Email()` validator |
| `scikit-learn` | 1.7.2 | TF-IDF + cosine similarity for recommendations |
| `scipy` | 1.15.3 | scikit-learn dependency |
| `numpy` | 1.24.3 | scikit-learn dependency — **pinned here only** |
| `pytest` | 9.1.1 | The test suite |

### 8.3 Install the AI stack (optional)

```powershell
pip install -r requirements-ai.txt
```

The exact pinned versions, from `requirements-ai.txt`:

| Package | Version | What it does |
| --- | --- | --- |
| `torch` | 2.10.0 | Runs the neural network. CPU build. |
| `transformers` | 4.57.1 | Loads and runs the Qwen model. **≥4.51 is a hard requirement** — that release added the `qwen3` architecture the model declares. |
| `peft` | 0.17.1 | Applies the LoRA adapter on top of the base model |
| `accelerate` | 1.14.0 | Trainer backend used by `peft` |
| `safetensors` | 0.8.0 | Weight loading — safetensors only, never Python `pickle` |
| `tokenizers` | 0.22.2 | Fast tokenizer |
| `huggingface_hub` | 0.36.2 | Local file resolution (never used to download — offline flags are set) |

**numpy is deliberately not re-pinned in this file.** It is pinned once, in
`requirements.txt`, at `1.24.3`. The comment in `requirements-ai.txt` explains that an
earlier version pinned two different numpy versions across the two files, which made
them mutually uninstallable. Do not add a numpy line here.

> **These are the supported AI dependency versions, verified end to end on
> 2026-08-20** in a clean Python 3.10.11 virtual environment built from
> `requirements.txt` + `requirements-ai.txt` with no overrides:
>
> ```text
> Python:       3.10.11
> torch:        2.10.0+cpu
> transformers: 4.57.1
> peft:         0.17.1
> accelerate:   1.14.0
> safetensors:  0.8.0
> tokenizers:   0.22.2
> huggingface_hub: 0.36.2
> numpy:        1.24.3
> ```
>
> `pip check` → *No broken requirements found.* The V4 adapter loaded through
> `app/services/local_llm.py::_load()` as `PeftModelForCausalLM` (rank 8, alpha 16,
> 5,046,272 LoRA parameters — matching `training_metrics.json`), with no fallback
> warning. Full suite: 420 passed, 0 failed, 0 skipped.

### 8.4 ⚠ Known environment drift on the original development machine

**Read this if you are taking over the original machine rather than building a fresh
one.**

A readiness audit on 2026-08-20 found that the installed AI packages on the
development machine no longer match `requirements-ai.txt`:

| Package | Pinned | Found installed |
| --- | --- | --- |
| `peft` | 0.17.1 | **not installed at all** |
| `transformers` | 4.57.1 | 5.15.0 |
| `torch` | 2.10.0 | 2.13.0+cpu |
| `numpy` | 1.24.3 | 2.2.6 |
| `huggingface_hub` | 0.36.2 | 1.27.0 |

The consequence is quiet and easy to miss: with `peft` absent,
`app/services/local_llm.py` logs

```text
WARNING [app.services.local_llm] Could not load LoRA adapter; continuing with base model
ModuleNotFoundError: No module named 'peft'
```

and falls back to the **raw base model**. The application keeps working, so nothing
looks broken — but the fine-tuned V4 behaviour is not what you are seeing.

Installing the pinned `peft==0.17.1` **on top of the drifted `transformers==5.15.0`**
fails, which is what makes this confusing to diagnose:

```text
ImportError: cannot import name 'HybridCache' from 'transformers'
```

`peft` 0.17.1 imports `HybridCache` from `transformers`; that symbol was removed in
transformers 5.0. **This is not a defect in the pins.** It is what happens when one
pinned package is reinstalled against another package that has drifted away from its
pin. `requirements-ai.txt` pins `transformers==4.57.1`, so installing the file as
written never produces this combination.

**The fix is to restore the pinned environment, not to bump `peft`.** Verified on
2026-08-20 in a clean virtual environment: `requirements.txt` + `requirements-ai.txt`
installed exactly as pinned, `pip check` reported no broken requirements, the V4
adapter loaded through the application's own loader, and the full suite passed
420/420.

```powershell
# Restore the pinned AI stack in the project virtual environment
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-ai.txt
pip check
```

If the drift is severe, the cleanest route is to delete `.venv` and rebuild it from
§7 — nothing of value lives there.

> **Do not "fix" this by installing a newer `peft`.** `peft==0.20.0` does import
> against `transformers` 5.15.0 and does load the adapter — it was tested — but it
> pairs the adapter with a stack that V4 was never trained or evaluated on, for no
> behavioural gain: on the pinned `transformers==4.57.1`, peft 0.17.1 and 0.20.0
> produce **byte-identical** explanations. Restoring the pins is the supported
> answer.

Either way, **confirm** the adapter loaded by checking the log at startup (§12.4).

### 8.5 Verify the installation

```powershell
pip check
```

Prints `No broken requirements found.` when the dependency graph is consistent.

A quick smoke test that does not need MySQL or the model files:

```powershell
pytest -q
```

This uses in-memory SQLite and never loads the model, so it is a genuine check that
the Python side of the install is sound **before** you touch the database.
See §12.5 for the expected result.

---

## 9 — Database migrations

This is the section that most often goes wrong, so it is the longest.

### 9.1 The four words you need

**SQLAlchemy model.** A Python class that describes one database table. Quick
Junction's live in `app/models/`. For example, `app/models/category.py`:

```python
class Category(db.Model):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80), unique=True, nullable=False)
    ...
```

That class *describes* a table. It does not create one.

**Alembic.** The tool that actually changes the database structure. It keeps a
numbered chain of change scripts and a record, inside your database, of which ones
have been applied.

**Flask-Migrate.** A small wrapper that connects Alembic to your Flask app so you can
type `flask db upgrade` instead of configuring Alembic by hand.

**A migration.** One Python file in `migrations/versions/` describing a single change
— "create the `orders` table", "add a column to `users`". Each file names the
migration that must come before it, forming a chain.

### 9.2 Why you must not create the tables by hand

You may notice that SQLAlchemy has a `db.create_all()` method that builds every table
from the models in one call. **Do not use it here**, and do not hand-write `CREATE
TABLE` statements.

Three reasons:

1. **Alembic would not know.** It tracks applied migrations in a table called
   `alembic_version`. Tables created outside that process leave the version table
   empty, so the next `flask db upgrade` tries to create everything again and fails
   with `Table 'x' already exists`.
2. **The migrations contain corrections the models alone do not express.** Migration
   `8d5ba0171efe` is literally titled *"Add menu data layer and fix missing enum CHECK
   constraints"*. Running `create_all()` skips that history.
3. **`db.create_all()` is used in this project, but only in the test suite.**
   `tests/conftest.py` calls it against a throwaway in-memory SQLite database. That is
   its only legitimate use here.

### 9.3 The migration chain in this repository

There are **five** migration files in `migrations/versions/`, forming a single
unbroken chain with **one head**:

```text
abf997064564   Add users and audit_logs tables                      (base — no parent)
      │
      ▼
8d5ba0171efe   Add menu data layer and fix missing enum CHECK constraints
      │        (categories, menu_items, ingredients, menu_item_ingredients,
      │         customer_preferences)
      ▼
d8f3bfd0e2a9   add cart order tables
      │        (creates `orders` and `order_items` — despite the name, it
      │         creates no cart table, because the cart is session-based)
      ▼
38297b707b89   widen audit event allow-list for order status events
      │
      ▼
2d9f3b20045f   add users.session_version for session revocation      ◄── HEAD
```

**The head revision is `2d9f3b20045f`.** `HANDOFF.md` states the same value, and a
live check on 2026-08-20 confirmed it.

### 9.4 Check where you are

```powershell
flask --app run.py db current
```

**On a brand-new empty database**, this prints nothing at all (or just log lines). That
is expected — no migration has been applied yet.

**On a fully migrated database**, it prints:

```text
2d9f3b20045f (head)
```

The `(head)` marker means you are on the newest migration in the chain.

> **Why `--app run.py`?** Flask needs to know where your application object lives.
> `run.py` creates it at module level (`app = create_app()`), so pointing Flask at
> that file is enough. You can avoid typing it every time by setting
> `$env:FLASK_APP = "run.py"` for the session.

### 9.5 Apply the migrations

```powershell
flask --app run.py db upgrade
```

**What this command does, step by step:**

1. Loads your Flask application, which reads `.env` and therefore `DATABASE_URL`.
2. Connects to MySQL as `qj_user`.
3. Reads the `alembic_version` table to find where the database currently is. If the
   table does not exist, it creates it and treats the database as being at "nothing
   applied".
4. Walks the chain from that point to the head, running each migration's `upgrade()`
   function in order.
5. Updates `alembic_version` after each one.

On a fresh database you will see five `Running upgrade` lines, ending with:

```text
INFO  [alembic.runtime.migration] Running upgrade 38297b707b89 -> 2d9f3b20045f, add users.session_version for session revocation
```

Confirm afterwards:

```powershell
flask --app run.py db current
```

Must now print `2d9f3b20045f (head)`.

### 9.6 Inspect the history

```powershell
flask --app run.py db history
```

Prints the chain, newest first, in the form `<parent> -> <revision> (head), <message>`.

```powershell
flask --app run.py db heads
```

Prints every head. **You should see exactly one line: `2d9f3b20045f (head)`.** More
than one line means the chain has branched — see §9.8.

### 9.7 ⚠ Never delete the `migrations/` directory

> **Deleting `migrations/` to "fix" a database problem destroys the project's entire
> schema history.**

It looks tempting: the migrations are erroring, deleting them makes the error go away.
What actually happens is that you lose the ability to upgrade any existing database
ever again, including the one holding your orders. Every other copy of the project
becomes unupgradeable. The corrections embedded in migration `8d5ba0171efe` are gone.

If your *local development* database is in a state you cannot untangle and the data is
disposable, the correct move is to reset the **database**, not the migrations:

```sql
DROP DATABASE quick_junction;
CREATE DATABASE quick_junction CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

then re-run `flask --app run.py db upgrade` and `python scripts/seed_demo.py`.
**Take a backup first (§16) if there is any doubt.**

### 9.8 Specific migration errors

#### `Target database is not up to date`

**Meaning:** the database is behind the newest migration. Alembic refuses to
autogenerate a new migration on top of a stale database, because the diff would be
meaningless.

**Fix:**

```powershell
flask --app run.py db upgrade
```

#### `Table 'quick_junction.xxx' doesn't exist` / `no such table`

**Meaning:** the application is querying a table that has not been created. Almost
always, migrations were never applied.

**Fix:**

```powershell
flask --app run.py db current   # likely prints nothing
flask --app run.py db upgrade
flask --app run.py db current   # must now print 2d9f3b20045f (head)
```

**If `db current` already says `2d9f3b20045f (head)` but the table is genuinely
missing**, then `alembic_version` is lying — someone created it, or the tables were
made by hand and then partially dropped. Verify with:

```sql
USE quick_junction;
SHOW TABLES;
```

You should see ten tables (§10.1). If they are missing, the cleanest fix on a
disposable database is the drop-and-recreate in §9.7.

#### `Multiple head revisions are present`

**Meaning:** two migration files claim the same parent, so the chain has forked. This
happens when two people generate a migration from the same starting point.

**This repository has a single head.** If you see this error, something has been added
locally. Check:

```powershell
flask --app run.py db heads
```

**Fix:** do not delete anything. Either upgrade to a specific head:

```powershell
flask --app run.py db upgrade 2d9f3b20045f
```

or merge the branches, which creates a new migration joining them:

```powershell
flask --app run.py db merge -m "merge heads" <head1> <head2>
```

Then `flask --app run.py db upgrade`.

#### `Can't locate revision identified by '<hash>'`

**Meaning:** `alembic_version` names a migration that does not exist in
`migrations/versions/`. Usually the database was migrated by a newer copy of the code
than the one you have checked out.

**Fix:** get the matching code (`git pull`, or ask for the current branch). Do not
hand-edit `alembic_version` unless you know exactly what you are doing.

---

## 10 — Database tables

Ten tables, described from `app/models/` and `migrations/versions/`.

### 10.1 Overview

| Table | Purpose |
| --- | --- |
| `users` | Accounts, roles, password hashes, session revocation counter |
| `categories` | Menu sections — Starters, Mains, Sides |
| `menu_items` | Dishes: price, cuisine, spice level, dietary type, availability |
| `ingredients` | Reference list of ingredient names |
| `menu_item_ingredients` | Join table: which ingredients each dish contains |
| `customer_preferences` | One row per customer: diet, cuisine, spice |
| `orders` | One placed order: owner, status, subtotal, total |
| `order_items` | One line of an order, with the price frozen at checkout |
| `audit_logs` | Append-only security and business event trail |
| `alembic_version` | Alembic's own bookkeeping — do not touch |

### 10.2 Relationship overview

```text
User (users)
 ├── CustomerPreference (customer_preferences)   one-to-one, user_id UNIQUE
 └── Order (orders)                              one-to-many
       └── OrderItem (order_items)               one-to-many
             └── MenuItem (menu_items)           each line references the dish

Category (categories)
 └── MenuItem (menu_items)                       one-to-many

MenuItem (menu_items)
 └── Ingredient (ingredients)                    many-to-many
       via MenuItemIngredient (menu_item_ingredients)

AuditLog (audit_logs)
 └── User (users)                                optional; NULL when no account applies

Cart                                             NOT A TABLE — session cookie only
```

> **Note the last line.** There is no cart table. `app/utils/cart.py` stores the cart
> as `{"<menu_item_id>": quantity}` inside the signed session cookie. The migration
> file named `d8f3bfd0e2a9_add_cart_order_tables.py` creates only `orders` and
> `order_items` — its name is misleading.

### 10.3 `users`

Defined in `app/models/user.py`.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | INT PK | |
| `username` | VARCHAR(32) UNIQUE | Stored lowercased by `normalize_username()` so uniqueness does not depend on collation |
| `email` | VARCHAR(255) UNIQUE | Stored lowercased by `normalize_email()` |
| `password_hash` | VARCHAR(255) | Argon2id encoded hash, ~100 chars; 255 leaves headroom for future parameter changes without a migration |
| `role` | VARCHAR(16) + CHECK | One of `admin`, `staff`, `customer`. Default `customer` |
| `is_active` | BOOLEAN | Default true. A deactivated account's session is dropped on its next request |
| `session_version` | INT | Default 0. **The mechanism that makes logout actually revoke** — see below |
| `created_at`, `updated_at` | DATETIME | Server-generated |

**Roles.** Three fixed roles, stored as a constrained string rather than a separate
table, because they have no per-role attributes worth a join.

**`session_version` — why it exists.** Flask's session is a *signed cookie* with no
server-side store. `session.clear()` on logout clears only the browser's copy; a
cookie captured beforehand kept working. This integer counter is the server-side half:
`login_user()` copies the current value into the session, every request compares the
two, and `logout_user()` increments the column — so every session issued before that
logout stops validating. It is a counter, not a token, so nothing secret is stored in
it or in the cookie. Added by migration `2d9f3b20045f`.

Logging out revokes **all** of that user's sessions, on every device. That is
deliberate and documented in `docs/SECURITY.md` §6.

### 10.4 `categories`

Defined in `app/models/category.py`.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | INT PK | |
| `name` | VARCHAR(80) UNIQUE | |
| `description` | VARCHAR(500) NULL | |
| `is_active` | BOOLEAN | Default true |
| `created_at`, `updated_at` | DATETIME | |

**Categories are deactivated, never deleted.** An inactive category hides its items
from the public menu without destroying `order_items` rows that reference those items.
The admin categories page says this in its own subtitle. The foreign key from
`menu_items.category_id` uses `ondelete="RESTRICT"`, so the database itself refuses to
delete a category that still has items.

### 10.5 `menu_items`

Defined in `app/models/menu_item.py`. This is the richest table.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | INT PK | |
| `category_id` | INT FK → `categories.id` | `ON DELETE RESTRICT` |
| `name` | VARCHAR(120) | |
| `description` | VARCHAR(2000) NULL | |
| `price` | **DECIMAL(10,2)** | `Decimal` in Python, never `float` |
| `cuisine` | VARCHAR(20) + CHECK | `indian`, `chinese`, `italian`, `continental`, `mexican`, `thai`, `multi_cuisine`, `other` |
| `spice_level` | VARCHAR(16) + CHECK | `none`, `mild`, `medium`, `hot`, `extra_hot` |
| `dietary_type` | VARCHAR(20) + CHECK | `vegetarian`, `vegan`, `eggetarian`, `non_vegetarian` |
| `is_available` | BOOLEAN | Default true. An unavailable item is hidden from the menu and **can never be recommended or ordered** |
| `created_at`, `updated_at` | DATETIME | |

**Constraints and indexes:**

* `CHECK (price > 0)` named `ck_menu_items_price_positive` — even a careless direct
  `INSERT` cannot create a free or negative-priced dish.
* Composite index `ix_menu_items_category_available` on `(category_id, is_available)`,
  backing the public menu's most common query.

**The three enum columns are the heart of the product.** They are the *same* Python
enums that `customer_preferences` uses (`app/models/enums.py`), not a parallel set.
That is what lets the recommendation engine compare a customer's preference against a
dish's attribute directly, with no translation layer.

### 10.6 The enum column pattern

Worth understanding, because it appears on six columns and is easy to get wrong.

These are **not** MySQL native `ENUM` types. They are `VARCHAR` columns with a `CHECK`
constraint, built by the `enum_column()` helper in `app/models/enums.py`. Four settings
have to be right together, and SQLAlchemy defaults three of them the other way:

| Setting | Why |
| --- | --- |
| `native_enum=False` | A VARCHAR, so adding a value later is a plain constraint migration rather than an `ALTER TYPE` |
| `create_constraint=True` | **Not the default in SQLAlchemy 2.0.** Without it you silently get a bare VARCHAR with no database-level enforcement at all |
| `values_callable=...` | Stores `member.value` (`"admin"`) rather than `member.name` (`"ADMIN"`) |
| an explicit `name=` | **MySQL requires `CHECK` constraint names to be unique per schema.** `Cuisine` backs both `menu_items.cuisine` and `customer_preferences.cuisine_preference`; without distinct names the second `CREATE TABLE` fails outright. SQLite tolerates the clash, so this is a bug that only shows up on MySQL |

The convention is `ck_<table>_<column>`.

### 10.7 `ingredients` and `menu_item_ingredients`

`ingredients` (`app/models/ingredient.py`) is a lookup table: `id`, `name`
(VARCHAR(80) UNIQUE), `created_at`. It has no `updated_at` because it is reference
data, not a mutable business entity.

`menu_item_ingredients` (`app/models/menu_item_ingredient.py`) is a pure join table
with only two columns, both foreign keys, forming a **composite primary key**. That
single choice does two jobs: it enforces "a dish lists an ingredient at most once",
and it is the table's only index. Both foreign keys use `ON DELETE CASCADE`, so
removing a dish or an ingredient cleans up the association automatically.

### 10.8 `customer_preferences`

Defined in `app/models/customer_preference.py`.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | INT PK | |
| `user_id` | INT FK → `users.id`, **UNIQUE** | `ON DELETE CASCADE`. Unique makes this one-to-one |
| `dietary_preference` | VARCHAR(20) + CHECK, **NULLABLE** | |
| `cuisine_preference` | VARCHAR(20) + CHECK, **NULLABLE** | |
| `spice_preference` | VARCHAR(16) + CHECK, **NULLABLE** | |
| `created_at`, `updated_at` | DATETIME | |

**All three columns are nullable, and that is load-bearing.** `NULL` means *"the
customer has stated no preference on this dimension"* — a meaningful answer, not
missing data, and deliberately not encoded as a sentinel enum member.

This distinction drives real behaviour further up the stack. The AI preference
safeguard in `app/services/local_llm.py` exists specifically to stop the language
model from telling a customer they asked for something when the corresponding column
is `NULL`.

The row is created **lazily** — the first time a customer saves anything. A customer
who has never opened the preferences page has no row at all, and the recommendation
engine handles that as an ordinary case.

### 10.9 `orders`

Defined in `app/models/order.py`.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | INT PK | |
| `user_id` | INT FK → `users.id` | `ON DELETE RESTRICT` — an order is a financial record and must never be orphaned or removed as a side effect |
| `status` | VARCHAR(16) + CHECK | `pending`, `confirmed`, `preparing`, `ready`, `completed`, `cancelled`. Default `pending` |
| `subtotal` | DECIMAL(10,2) | `CHECK (subtotal >= 0)` |
| `total` | DECIMAL(10,2) | `CHECK (total >= 0)`. Currently equal to `subtotal` — no tax, discount or delivery fee exists yet. Kept as its own column so a future fee milestone has somewhere to diverge without a schema change |
| `created_at`, `updated_at` | DATETIME | |

Composite index `ix_orders_user_created` on `(user_id, created_at)`, backing the only
query `GET /orders` runs.

**The status workflow is an explicit allow-list**, enforced server-side in
`app/services/orders.py`:

```text
pending    → confirmed, cancelled
confirmed  → preparing, cancelled
preparing  → ready, cancelled
ready      → completed
completed  → (terminal — nothing)
cancelled  → (terminal — nothing)
```

Anything not on that list is rejected, including same-status "changes" and every
backwards step. The staff UI renders only the legal buttons as a convenience, and
`update_order_status` re-checks regardless of what was posted.

### 10.10 `order_items`

Defined in `app/models/order.py`.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | INT PK | |
| `order_id` | INT FK → `orders.id` | `ON DELETE CASCADE` — deleting an order removes its lines |
| `menu_item_id` | INT FK → `menu_items.id` | `ON DELETE RESTRICT` |
| `item_name_snapshot` | VARCHAR(120) | **Frozen at checkout** |
| `unit_price_snapshot` | DECIMAL(10,2) | **Frozen at checkout** |
| `quantity` | INT | `CHECK (quantity > 0)` |
| `line_total` | DECIMAL(10,2) | `CHECK (line_total >= 0)` |

**The snapshot columns are the most important business rule in the schema.** When a
dish is renamed or repriced, historical orders must not silently change. Every price on
an order is computed server-side from the live `menu_items` row *at checkout time* and
then frozen here. A client-submitted price, subtotal or total is never accepted as
authoritative anywhere in the codebase.

The whole checkout — validate, resolve, price, write — runs as a single transaction.
Either a complete correctly-priced order and all its lines commit together, or nothing
is written at all.

### 10.11 `audit_logs`

Defined in `app/models/audit_log.py`. **Append-only.** The only writer is
`app/services/audit.py`.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | INT PK | |
| `event_type` | VARCHAR(32) + CHECK, indexed | 17 allowed values (below) |
| `user_id` | INT FK → `users.id` NULL, indexed | `ON DELETE SET NULL`, so the audit row survives account deletion. NULL when no account applies — e.g. a failed login for a username that does not exist |
| `success` | BOOLEAN | |
| `ip_address` | VARCHAR(45) NULL | 45 = maximum IPv6 length |
| `user_agent` | VARCHAR(255) NULL | |
| `metadata_json` | TEXT NULL | Small non-sensitive context only |
| `created_at` | DATETIME, indexed | |

The 17 event types: `register_success`, `login_success`, `login_failure`,
`login_rate_limited`, `logout`, `category_created`, `category_updated`,
`category_deactivated`, `menu_item_created`, `menu_item_updated`,
`menu_item_availability_changed`, `ingredient_changed`, `order_created`,
`order_creation_failed`, `order_status_changed`, `order_status_change_rejected`.

> **Treat these rows as sensitive.** They contain source IP addresses and login
> activity. The model's own docstring recommends a defined retention window (90 days
> as a starting point) with access restricted to operators. **Retention is not
> enforced in code** — `docs/SECURITY.md` §17 lists it as outstanding work.

**Never a password, hash, token, or full request body.** That rule is enforced by the
service being the only writer.

---

## 11 — Seed demo data

### 11.1 What the script is for

`scripts/seed_demo.py` populates an empty database with enough content to click
around: three accounts, three categories, and eighteen menu items with a deliberate
spread of cuisines, spice levels and dietary types.

Its own docstring is explicit about its status:

```text
**Development convenience only.** It refuses to run against a production
configuration, and the passwords it creates are obvious placeholders that
would never satisfy a real deployment. It is idempotent: run it twice and
nothing is duplicated.
```

### 11.2 Run it

```powershell
python scripts/seed_demo.py
```

### 11.3 What it creates

**Three accounts**, one per role:

| Username | Email | Role |
| --- | --- | --- |
| `customer` | `customer@example.com` | customer |
| `staff` | `staff@example.com` | staff |
| `admin` | `admin@example.com` | admin |

All three are created with the same password, which is written in plain text at the
top of the script:

```text
DEMO_PASSWORD = "demo-password-1"
```

> ### 🔴 DEMO ONLY — DO NOT USE AS PRODUCTION CREDENTIALS
>
> `demo-password-1` is **public**. It is committed to this repository in
> `scripts/seed_demo.py`, so it is known to anyone who has ever seen the source.
>
> * Fine for local development and for a demonstration on a laptop.
> * **Never** acceptable on any machine reachable by anyone else.
> * Before any real deployment: change these passwords, or better, **delete the demo
>   accounts entirely.** `HANDOFF.md` states this as a rule, not a suggestion.

**Three categories:** `Starters`, `Mains`, `Sides`.

**Eighteen menu items.** The first ten are the original set; the last eight were added
later so that changing a preference visibly changes the recommendation. One is
deliberately marked unavailable:

| Item | Category | Price | Cuisine | Diet | Spice | Available |
| --- | --- | --- | --- | --- | --- | --- |
| Paneer Tikka | Starters | 249.00 | Indian | vegetarian | hot | ✅ |
| Chilli Paneer | Starters | 269.00 | Chinese | vegetarian | hot | ✅ |
| Garden Salad | Starters | 149.00 | Continental | vegan | none | ✅ |
| Butter Chicken | Mains | 349.00 | Indian | non-vegetarian | medium | ✅ |
| Chana Masala | Mains | 229.00 | Indian | vegan | hot | ✅ |
| Margherita Pizza | Mains | 399.00 | Italian | vegetarian | none | ✅ |
| Thai Green Curry | Mains | 379.00 | Thai | non-vegetarian | extra_hot | ✅ |
| Egg Fried Rice | Sides | 199.00 | Chinese | eggetarian | mild | ✅ |
| Steamed Rice | Sides | 99.00 | Chinese | vegan | none | ✅ |
| **Sold Out Biryani** | Mains | 329.00 | Indian | non-vegetarian | hot | ❌ **deliberately unavailable** |
| Black Bean Tacos | Mains | 319.00 | Mexican | vegan | medium | ✅ |
| Chicken Fajitas | Mains | 389.00 | Mexican | non-vegetarian | mild | ✅ |
| Som Tam Salad | Starters | 239.00 | Thai | vegan | extra_hot | ✅ |
| Mushroom Risotto | Mains | 409.00 | Italian | vegetarian | none | ✅ |
| Shepherd's Pie | Mains | 419.00 | Continental | non-vegetarian | mild | ✅ |
| Falafel Wrap | Starters | 229.00 | Other | vegan | medium | ✅ |
| Shakshuka | Starters | 259.00 | Other | eggetarian | mild | ✅ |
| Buddha Bowl | Mains | 349.00 | Multi Cuisine | vegan | none | ✅ |

Ingredients are created and linked as a side effect, via
`sync_menu_item_ingredients()`.

**Sold Out Biryani exists on purpose.** It proves the availability filter: it is
visible on the admin menu list and invisible everywhere a customer looks.

### 11.4 What it does *not* create

**No orders.** The docstring explains why:

```text
Orders are deliberately *not* seeded -- placing one through the UI is part of
the demo, and a seeded order would hide the checkout flow.
```

**No customer preferences.** Setting those is part of the demo too.

### 11.5 Is it safe to run repeatedly?

**Yes.** It is idempotent. Every insert is guarded by a lookup first — categories by
name, menu items by name, users by username. Running it twice creates nothing new.

It also refuses to run against production:

```python
if app.config["ENV_NAME"] == "production":
    print("Refusing to seed demo data into a production configuration.", file=sys.stderr)
    return 1
```

### 11.6 Verify it worked

The script prints a summary:

```text
categories: 3
menu items: 18 (+18 new)
users:      3 (+3 new)

Demo accounts (development only):
  customer  / demo-password-1   role=customer
  staff     / demo-password-1   role=staff
  admin     / demo-password-1   role=admin
```

`(+N new)` shows how many were created on this run; a second run shows `(+0 new)`.

Cross-check directly in MySQL:

```sql
USE quick_junction;
SELECT COUNT(*) FROM categories;   -- 3
SELECT COUNT(*) FROM menu_items;   -- 18
SELECT COUNT(*) FROM users;        -- 3
SELECT username, role FROM users ORDER BY id;
```

---

## 12 — Verify the MySQL connection

### 12.1 Layer by layer

Test from the bottom up. If a layer fails, there is no point testing the one above it.

**Layer 1 — is MySQL running?**

```powershell
Get-Service -Name "MySQL*"
Test-NetConnection -ComputerName 127.0.0.1 -Port 3306
```

**Layer 2 — can the application account log in?**

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u qj_user -p -h 127.0.0.1 quick_junction
```

Landing at `mysql>` means the user, the password, the host and the database are all
correct. This one command eliminates most of §17.

**Layer 3 — can Python reach it?** (virtual environment activated)

```powershell
flask --app run.py db current
```

`2d9f3b20045f (head)` means Python read `.env`, built the DSN, loaded PyMySQL,
connected, authenticated, and read `alembic_version`. That is the entire database path
proven in one command.

### 12.2 Start the application

```powershell
python run.py
```

### 12.3 What success looks like

```text
2026-08-20 12:00:00,000 INFO     [app] Logging configured for development environment
 * Serving Flask app 'app'
 * Debug mode: on
 * Running on http://127.0.0.1:5000
Press CTRL+C to quit
```

Then open `http://127.0.0.1:5000/menu` in a browser. **If the menu shows dishes with
prices, MySQL connectivity is fully proven** — that page reads `menu_items`, joins
`categories`, and renders `DECIMAL` prices.

> **The landing page `/` also reads the database.** If `/menu` renders but `/` errors,
> that is not a database problem.

### 12.4 If you installed the AI stack

Watch the startup log for the adapter:

```text
INFO [app.services.local_llm] Local LLM warm-up started in the background
INFO [app.services.local_llm] Loaded LoRA adapter from ...\models\qwen3-0.6b-quickjunction-lora-v4
INFO [app.services.local_llm] Local LLM warmed up in 15.6s; first explanation will be fast
```

**The middle line is the one that matters.** If instead you see

```text
WARNING [app.services.local_llm] Could not load LoRA adapter; continuing with base model
```

the application is running the untuned base model. See §8.4.

If you see

```text
WARNING [app.services.local_llm] Local LLM disabled: model directory not found at ...
```

the `models/` directory is missing or `LLM_MODEL_PATH` points at the wrong place.
**Neither is fatal** — the app runs, and the explanation panel says so.

### 12.5 Run the test suite

```powershell
pytest -q
```

Measured on 2026-08-20 against this exact commit:

```text
420 tests   0 failures   0 errors   0 skipped   ~29 seconds
12 warnings
```

The 12 warnings are all the same one, a `DeprecationWarning` about `get_engine` in
`migrations/env.py`. It is harmless.

> **Older counts in other documents are out of date.** `README.md` §12 says 221 tests
> and `HANDOFF.md` says 378. The measured figure at this commit is **420**. If you are
> reconciling documentation, 420 is the number to trust — it came from a JUnit XML
> report produced by the run itself.

**About skips.** Four tests in `tests/test_dataset_v2.py`, `test_dataset_v3.py` and
`test_dataset_v4.py` call `pytest.skip(...)` when an adapter directory is absent, with
messages like *"V4 adapter not present in this checkout (models/ is git-ignored)"*.
On a machine that has `models/`, nothing skips — hence 0 skipped above. On a clean
checkout without the weights, those tests skip **by design** and the run still passes.
A skip there is expected, not a failure.

### 12.6 Common connection errors

#### `Access denied for user 'qj_user'@'localhost' (using password: YES)`

Wrong password, or the account does not exist for the host you connected from.

* Re-read §5.3 on `localhost` vs `127.0.0.1`.
* Check the password in `.env` matches what you set in `CREATE USER`.
* Check for special characters needing URL encoding (§6.5).
* Confirm the account exists:
  ```sql
  SELECT user, host FROM mysql.user WHERE user = 'qj_user';
  ```
* Reset the password if needed (as admin):
  ```sql
  ALTER USER 'qj_user'@'127.0.0.1' IDENTIFIED BY 'CHOOSE_A_STRONG_PASSWORD';
  FLUSH PRIVILEGES;
  ```

#### `Unknown database 'quick_junction'`

The database was never created (§4), or the name in `.env` is misspelled. Check:

```sql
SHOW DATABASES;
```

#### `Can't connect to MySQL server on '127.0.0.1'` / `Connection refused` (error 2003)

MySQL is not running, is on a different port, or is blocked.

```powershell
Get-Service -Name "MySQL*"
Start-Service -Name "MySQL80"
Test-NetConnection -ComputerName 127.0.0.1 -Port 3306
```

If MySQL was installed on a non-default port, update the port in `DATABASE_URL`.

#### `Authentication plugin 'caching_sha2_password' cannot be loaded`

Rare with PyMySQL 1.2.0, which supports the MySQL 8 default. If you hit it, switch
that one account to the older plugin (as admin):

```sql
ALTER USER 'qj_user'@'127.0.0.1'
  IDENTIFIED WITH mysql_native_password BY 'CHOOSE_A_STRONG_PASSWORD';
FLUSH PRIVILEGES;
```

This weakens the password-exchange mechanism slightly; prefer fixing the client if you
can.

#### `Lost connection to MySQL server during query` after the app has been idle

MySQL closes idle connections. The project already handles this in `config.py`:

```python
SQLALCHEMY_ENGINE_OPTIONS: dict = {"pool_pre_ping": True, "pool_recycle": 280}
```

`pool_pre_ping` tests a pooled connection before use; `pool_recycle: 280` discards
connections older than 280 seconds, which sits below MySQL's default `wait_timeout`.
If you still see this, your server's `wait_timeout` has been lowered below 280 —
raise it, or lower `pool_recycle`.

#### `ConfigError: missing required environment variable(s): DATABASE_URL`

`.env` is missing, is in the wrong directory, or has no `DATABASE_URL` line.
`config.py` loads `.env` from the **repository root** — the folder containing
`config.py`. Confirm:

```powershell
Test-Path .\.env
Get-Content .\.env | Select-String "^DATABASE_URL="
```

There is deliberately no default database URL; an unset `DATABASE_URL` fails
immediately rather than silently connecting somewhere unintended.

---

## 13 — Start Quick Junction

### 13.1 The supported command

```powershell
python run.py
```

That is it. `run.py` is nine lines of logic:

```python
app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_RUN_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_RUN_PORT", "5000")),
        debug=app.config["DEBUG"],
    )
```

### 13.2 Host, port, URL

| Setting | Default | Override |
| --- | --- | --- |
| Host | `127.0.0.1` | `FLASK_RUN_HOST` in `.env` |
| Port | `5000` | `FLASK_RUN_PORT` in `.env` |
| URL | **http://127.0.0.1:5000** | |

`127.0.0.1` means **this machine only**. Nothing on your network can reach it. That is
the right default: the development server has no TLS and the project has not yet added
security headers (`docs/SECURITY.md` §17).

> Setting `FLASK_RUN_HOST=0.0.0.0` exposes the development server to your whole local
> network, unencrypted, with demo accounts whose password is public. Do not do this.

### 13.3 Debug mode

Debug comes from the resolved configuration, never from an environment flag of its
own — so it **can only ever be on in the development environment**:

| `APP_ENV` | `DEBUG` |
| --- | --- |
| `development` | `True` |
| `testing` | `False` |
| `production` | `False`, and `ProductionConfig.validate()` raises if it is ever `True` |

With debug on you get automatic reload on file save and an interactive traceback in
the browser. Both are development-only conveniences.

### 13.4 What `APP_ENV` changes

| | `development` | `production` |
| --- | --- | --- |
| `DEBUG` | `True` | `False`, enforced |
| `SESSION_COOKIE_SECURE` | `False` — so the cookie survives plain HTTP on localhost | `True` |
| `LOG_LEVEL` | `DEBUG` | `INFO` |
| `LLM_WARMUP` | **on** | **off** by default |
| `SECRET_KEY` strength | not checked | must be ≥32 chars and not a placeholder |
| SQLite DSN | allowed | **rejected** |

`LLM_WARMUP` defaults on in development because that is the demo case and one process
means one model. It defaults off in production because each worker process warms its
own copy — N workers means N resident 1.2 GB models. `config.py` calls that "an
operator's decision, not a safe default".

### 13.5 Production

`run.py` is a **development** entry point. Its own docstring says so. For production
the project documents a WSGI server:

```bash
gunicorn "app:create_app('production')"
```

> **Note for Windows:** gunicorn does not run on Windows. On a Windows host you would
> need `waitress` or a Linux target. **This project has not been deployed to
> production**, and no deployment configuration for it exists in the repository.
> `docs/SECURITY.md` §17 lists TLS termination, security headers and a least-privilege
> database user as work still required before a real deployment. Do not treat the
> gunicorn line as a tested deployment recipe.

### 13.6 Stopping it

`Ctrl+C` in the terminal running it.

---

## 14 — First-time setup checklist

Print this. Tick as you go. Section references point back into this document.

```text
PREREQUISITES
[ ] Install Git                                              (§2.1, optional if given a ZIP)
[ ] Install Python 3.10+                                     (§2.1)
[ ] Verify: python --version   →  3.10 or newer              (§7.1)

MYSQL
[ ] Install MySQL Server 8.0+ (+ Workbench, recommended)     (§3.2, §3.3)
[ ] Set the MySQL admin password, store it safely            (§3.3)
[ ] Verify the MySQL service is Running                      (§3.4)
[ ] Connect with the mysql client                            (§3.5)
[ ] Verify: SELECT VERSION();  →  8.0.x or newer             (§3.6)

DATABASE
[ ] CREATE DATABASE quick_junction (utf8mb4/unicode_ci)      (§4.3 or §4.4)
[ ] Verify charset and collation                             (§4.3)
[ ] CREATE USER 'qj_user'@'127.0.0.1' AND @'localhost'       (§5.4)
[ ] GRANT ALL PRIVILEGES ON quick_junction.* to both         (§5.4)
[ ] FLUSH PRIVILEGES                                         (§5.4)
[ ] Verify: SHOW GRANTS FOR 'qj_user'@'127.0.0.1';           (§5.4)

PROJECT
[ ] Clone or copy the repository
[ ] Create the virtual environment: python -m venv .venv     (§7.2)
[ ] Activate it: .\.venv\Scripts\Activate.ps1                (§7.2)
[ ] Verify (Get-Command python).Source points into .venv     (§7.3)
[ ] pip install -r requirements.txt                          (§8.2)
[ ] Verify: pip check                                        (§8.5)

CONFIGURATION
[ ] Copy-Item .env.example .env                              (§6.1)
[ ] Generate SECRET_KEY and paste it in                      (§6.3)
[ ] Set DATABASE_URL with your qj_user password              (§6.4)
[ ] URL-encode special characters in the password if needed  (§6.5)
[ ] Confirm .env is NOT tracked by Git                       (§6.1)

DATABASE STRUCTURE
[ ] flask --app run.py db current   →  (nothing yet)         (§9.4)
[ ] flask --app run.py db upgrade                            (§9.5)
[ ] flask --app run.py db current   →  2d9f3b20045f (head)   (§9.5)
[ ] Verify: SHOW TABLES;  →  10 tables                       (§10.1)

DEMO DATA
[ ] python scripts/seed_demo.py                              (§11.2)
[ ] Verify: 3 categories, 18 menu items, 3 users             (§11.6)

AI  — SKIP THIS WHOLE BLOCK IF YOU DO NOT NEED AI EXPLANATIONS
[ ] pip install -r requirements-ai.txt                       (§8.3)
[ ] Obtain models/ from the project owner (~1.4 GB)          (§2.4)
[ ] Place Qwen3-0.6B-Base/ and
    qwen3-0.6b-quickjunction-lora-v4/ under models/          (§2.4)
[ ] Optional: python scripts/smoke_local_model.py

RUN
[ ] python run.py                                            (§13.1)
[ ] Confirm "Running on http://127.0.0.1:5000"               (§12.3)
[ ] If AI installed: confirm "Loaded LoRA adapter from ...v4"(§12.4)

VERIFY IN THE BROWSER
[ ] Open http://127.0.0.1:5000/           landing page loads
[ ] Open /menu                            18 items minus 1 unavailable = 17 shown
[ ] Log in as customer / demo-password-1                     (§11.3)
[ ] /preferences   — set diet, cuisine, spice, save
[ ] /recommendations — ranked list with match percentages
[ ] /recommendations/explain — AI panel (or the graceful notice)
[ ] Add an item to the cart; cart badge increments
[ ] /checkout → Place order
[ ] /orders — the order appears
[ ] Log in as staff — /staff/orders shows the queue
[ ] Log in as admin  — /admin/menu shows all 18 items

FINAL
[ ] pytest -q   →  420 passed                                (§12.5)
[ ] Confirm no secret was committed: git status               (§16.5)
[ ] Take a first backup                                       (§16.2)
```

---

## 15 — New machine verification

A 10–15 minute end-to-end check to run once installation is complete. If every step
passes, the handover is sound.

### Minutes 0–2: environment

```powershell
python --version                    # 3.10 or newer
(Get-Command python).Source         # must end in \.venv\Scripts\python.exe
pip check                           # No broken requirements found.
Get-Service -Name "MySQL*"          # Running
```

### Minutes 2–4: database

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u qj_user -p -h 127.0.0.1 quick_junction
```

At the `mysql>` prompt:

```sql
SELECT VERSION();
SHOW TABLES;
SELECT COUNT(*) FROM menu_items;
SELECT COUNT(*) FROM users;
exit;
```

Expect MySQL 8.0+, ten tables, 18 menu items, 3 users.

### Minutes 4–5: migrations

```powershell
flask --app run.py db current       # 2d9f3b20045f (head)
flask --app run.py db heads         # exactly one line
```

### Minutes 5–6: tests

```powershell
pytest -q
```

Expect **420 passed**, 0 failures, 12 warnings. This needs neither MySQL nor the model
files.

### Minutes 6–7: startup

```powershell
python run.py
```

Confirm:
- `Logging configured for development environment`
- `Running on http://127.0.0.1:5000`
- with AI installed: `Loaded LoRA adapter from ...qwen3-0.6b-quickjunction-lora-v4`

### Minutes 7–8: public pages

| URL | Expect |
| --- | --- |
| `http://127.0.0.1:5000/` | Landing page, "Order from the Quick Junction kitchen" |
| `/menu` | Menu cards with ₹ prices and category filters |
| `/health` | `{"status":"ok"}` |
| `/this-does-not-exist` | Styled 404 page, not a raw traceback |

### Minutes 8–9: authentication

1. Open `/recommendations` while logged out → redirected to `/login` with
   *"Please log in to continue."*
2. Log in as `customer` / `demo-password-1` → lands on `/account/`.
3. Deliberately enter a wrong password → *"Invalid username or password."*
4. Enter a username that does not exist → **the identical message**. It must not
   reveal which field was wrong.

### Minutes 9–11: preferences and recommendations

1. `/preferences` → set **Cuisine: Indian**, **Diet: Vegetarian**, **Spice: Hot** →
   Save.
2. You are redirected to `/recommendations`.
3. Expect **Paneer Tikka** at the top with roughly a **35 % "Good match"**, followed by
   Chana Masala and Chilli Paneer.
4. Confirm **no non-vegetarian dish appears anywhere on the page.** The dietary filter
   is a hard filter, not a ranking hint — this is the single most important behaviour
   to verify.

### Minutes 11–13: the AI explanation

Click **"Why the top pick?"** (`/recommendations/explain`).

**With the model installed and the adapter loaded**, expect a two-panel page. The left
panel lists the facts; the right shows a short sentence. For the preferences above, the
V4 adapter produces:

> *"Paneer Tikka is Indian, vegetarian and hot, matching all three preferences you set."*

Generation takes about **6 seconds** on CPU once warmed up. That is normal.

**Without the model**, expect:

> *"AI explanation temporarily unavailable. The recommendation itself is unaffected —
> it is calculated without the model."*

**Both outcomes are a pass.** The second one is the graceful-degradation path working
correctly.

### Minutes 13–15: ordering, staff, admin

1. Click **View this item** → **Add to cart**. The navbar cart badge shows `1`.
2. `/cart` → line total correct → **Go to checkout**.
3. `/checkout` → **Place order**.
4. `/orders` → the order appears with status **pending**.
5. Log out. Log in as `staff` / `demo-password-1`.
6. `/staff/orders` → the new order is at the top. Open it. Only **Mark cancelled** and
   **Mark confirmed** are offered — the state machine at work.
7. Log out. Log in as `admin` / `demo-password-1`.
8. `/admin/menu` → all **18** items, including **Sold Out Biryani** marked
   *Unavailable*.

**Authorization spot-check:** while logged in as `customer`, type
`http://127.0.0.1:5000/admin/menu` into the address bar. You must get a **403**
page. Roles are enforced on the route, not by hiding navbar links.

---

## 16 — Backup and recovery

### 16.1 What matters, and what does not

| Item | Back up? | Why |
| --- | --- | --- |
| **MySQL database** | **YES — critical** | Users, orders, audit trail. **Irreplaceable.** |
| **`.env`** | **YES — separately and securely** | Holds the DB password and `SECRET_KEY`. Not in Git, so nothing else protects it. Put it in a password manager or an encrypted store — **never** in the repository or a shared drive |
| **`models/`** | **YES** | ~1.4 GB, not in Git, and there is **no documented download source**. The base model came from an external source; the V4 adapter is a *product of this project*. If it is lost, it can only be reproduced by re-running training (~62 minutes on CPU) from `data/raw/seed_examples_v4.jsonl` |
| **Source code** | Covered by Git | Push to your remote. That *is* the backup |
| **`migrations/`** | Covered by Git | Part of the source. **Never delete it** (§9.7) |
| **`data/`** | Covered by Git | The hand-authored datasets are committed |
| **`.venv/`** | **No** | Rebuild it with `pip install -r requirements.txt` |
| **`logs/`** | Optional | Operational, not business data. Rotated automatically (2 MB × 5 files) |
| **`__pycache__/`, `.pytest_cache/`** | **No** | Regenerated automatically |

### 16.2 Backing up the database

Use `mysqldump`, which ships with MySQL. **Verify the database name first** —
this project uses `quick_junction`:

```sql
SHOW DATABASES LIKE 'quick_junction';
```

Then, from PowerShell:

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe" `
    -u MYSQL_ADMIN_USER -p `
    --single-transaction `
    --routines `
    --triggers `
    --default-character-set=utf8mb4 `
    quick_junction > "C:\backups\quick_junction_2026-08-20.sql"
```

| Flag | Why |
| --- | --- |
| `-p` (no password after it) | Prompts instead of putting the password in your shell history |
| `--single-transaction` | Takes a consistent snapshot of InnoDB tables without locking them, so the app can keep running |
| `--routines`, `--triggers` | Included for completeness; this schema has none today, but a future one might |
| `--default-character-set=utf8mb4` | **Do not omit this.** Without it, the ₹ symbol and any non-Latin text can be mangled in the dump file |

Give backups dated filenames. Overwriting one backup with another is how people
discover their only copy is also corrupt.

### 16.3 Restoring

Create an empty database with the right character set, then load the dump:

```sql
CREATE DATABASE quick_junction
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" `
    -u MYSQL_ADMIN_USER -p `
    --default-character-set=utf8mb4 `
    quick_junction < "C:\backups\quick_junction_2026-08-20.sql"
```

Then bring the structure up to date, in case the dump predates a migration:

```powershell
flask --app run.py db upgrade
flask --app run.py db current    # 2d9f3b20045f (head)
```

Verify the restore:

```sql
USE quick_junction;
SHOW TABLES;
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM orders;
SELECT COUNT(*) FROM menu_items;
```

> **A backup you have never restored is not a backup.** Practise a restore into a
> throwaway database named something like `quick_junction_restore_test` before you
> need it for real.

### 16.4 Backing up the model files

Plain file copy — nothing clever needed:

```powershell
Compress-Archive -Path .\models\Qwen3-0.6B-Base, .\models\qwen3-0.6b-quickjunction-lora-v4 `
                 -DestinationPath "C:\backups\quick_junction_models.zip"
```

The two directories above are the only ones **required**. The older adapters
(`-lora`, `-v2`, `-v3`) are historical and can be archived separately or left out.

### 16.5 Confirming no secret has been committed

Before every push:

```powershell
git status
git ls-files | Select-String -Pattern "^\.env$"
```

The second command must return **nothing**. If `.env` ever appears in
`git ls-files`, it is tracked — stop, rotate the `SECRET_KEY` and the database
password immediately, then remove the file from history. Rotate first, clean second;
a secret that has been committed is a secret that has leaked.

---

## 17 — Troubleshooting

Every entry below is based on actual behaviour of this project, verified in the source.

| Problem | Likely cause | Fix |
| --- | --- | --- |
| **`ConfigError: missing required environment variable(s): DATABASE_URL`** | `.env` missing, in the wrong folder, or has no `DATABASE_URL` line. There is deliberately no default | Create `.env` in the **repository root** (the folder with `config.py`). Copy from `.env.example` and fill it in (§6) |
| **`ConfigError: ... missing required environment variable(s): SECRET_KEY`** | Same file, missing key | Generate one: `python -c "import secrets; print(secrets.token_urlsafe(64))"` (§6.3) |
| **`ConfigError: SECRET_KEY is weak or a placeholder`** | `APP_ENV=production` with a key under 32 chars or on the deny-list (`change-me`, `secret`, the `.env.example` placeholder…) | Generate a real key (§6.3). For local work, `APP_ENV=development` does not enforce this |
| **`ConfigError: Production requires the MySQL DATABASE_URL`** | `APP_ENV=production` with a `sqlite://` DSN | Point `DATABASE_URL` at MySQL, or set `APP_ENV=development` |
| **Flask cannot connect to the DB — `Access denied for user`** | Wrong password; or the account exists for `localhost` but you connected via `127.0.0.1` (they are different accounts in MySQL) | Create both host forms (§5.3, §5.4). Check the password. URL-encode special characters (§6.5) |
| **`Unknown database 'quick_junction'`** | Database never created, or misspelled in `.env` | `SHOW DATABASES;` then create it per §4.3 |
| **`Can't connect to MySQL server on '127.0.0.1' (10061)`** | MySQL service stopped, wrong port, or firewall | `Get-Service -Name "MySQL*"`, then `Start-Service -Name "MySQL80"`. Check the port with `Test-NetConnection -Port 3306` |
| **`Authentication plugin 'caching_sha2_password' cannot be loaded`** | Client/plugin mismatch | `ALTER USER 'qj_user'@'127.0.0.1' IDENTIFIED WITH mysql_native_password BY '<password>';` then `FLUSH PRIVILEGES;` (§12.6) |
| **`Table 'quick_junction.users' doesn't exist` / `no such table`** | Migrations never applied | `flask --app run.py db upgrade`, then confirm `db current` says `2d9f3b20045f (head)` (§9.8) |
| **`Target database is not up to date`** | Database behind the newest migration | `flask --app run.py db upgrade` (§9.8) |
| **`Multiple head revisions are present`** | The migration chain has forked locally. **This repo ships a single head** | `flask --app run.py db heads`, then upgrade to a named head or merge. **Do not delete `migrations/`** (§9.7, §9.8) |
| **`Can't locate revision identified by '<hash>'`** | The database was migrated by newer code than you have checked out | Get the matching code. Do not hand-edit `alembic_version` (§9.8) |
| **`Lost connection to MySQL server during query`** after idling | MySQL closed an idle pooled connection | Already mitigated by `pool_pre_ping` + `pool_recycle: 280` in `config.py`. If it persists, your `wait_timeout` is below 280 s — raise it (§12.6) |
| **`Incorrect string value: '\xE2\x82\xB9...'`** | The database or a column is `utf8`, not `utf8mb4`. `\xE2\x82\xB9` is the ₹ sign | Recreate the database with `utf8mb4` / `utf8mb4_unicode_ci` (§4.2). Add `--default-character-set=utf8mb4` to any dump/restore |
| **AI panel says "AI explanation temporarily unavailable"** | `requirements-ai.txt` not installed, `models/` missing, or generation failed. **This is graceful degradation, not a crash** | Check `logs/app.log` for the reason. Install the AI stack (§8.3), place `models/` (§2.4). Everything else keeps working regardless |
| **Log says `Local LLM disabled: model directory not found at ...`** | `models/Qwen3-0.6B-Base` is absent, or `LLM_MODEL_PATH` points elsewhere | Place the directory (§2.4). If you set `LLM_MODEL_PATH`, use an **absolute** path — a relative one resolves against the current working directory, not the repo root |
| **Log says `Could not load LoRA adapter; continuing with base model`** | `peft` missing, or the installed `transformers` has drifted above the pin; or the adapter directory is absent | Restore the pinned stack: `pip install -r requirements.txt -r requirements-ai.txt`, then `pip check` (§8.4). **Explanations will be noticeably worse until this is fixed** |
| **`ImportError: cannot import name 'HybridCache' from 'transformers'`** | `peft==0.17.1` reinstalled against a drifted `transformers` 5.x. `HybridCache` was removed in transformers 5.0 | Restore the pinned `transformers==4.57.1` rather than bumping `peft` — `pip install -r requirements-ai.txt` (§8.4) |
| **`transformers` or `peft` raises an unrelated TensorFlow `ImportError`** | A broken TensorFlow install on the machine; `transformers` imports it opportunistically | The application already sets `USE_TF=0` / `USE_FLAX=0` / `USE_JAX=0` before importing. If you hit this in your own script, set them first too |
| **`Address already in use` / `Only one usage of each socket address` on port 5000** | Another process (often a previous `python run.py`) still holds the port | Find it: `Get-NetTCPConnection -LocalPort 5000 \| Select-Object OwningProcess`, then `Stop-Process -Id <pid>`. Or set `FLASK_RUN_PORT=5001` in `.env` |
| **PowerShell: "running scripts is disabled on this system"** | Execution policy blocks `Activate.ps1` | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` (§7.2) |
| **`pip install` fails with a compiler error** | You are not in the virtual environment, or Python is older than 3.10 | Activate `.venv` (§7.2) and check `python --version` (§7.1) |
| **`flask: command not found`** | Virtual environment not activated | `.\.venv\Scripts\Activate.ps1`. Check for the `(.venv)` prompt prefix |
| **Menu page is empty** | Demo data never seeded, or every category is inactive | `python scripts/seed_demo.py` (§11.2). Check: `SELECT COUNT(*) FROM menu_items;` |
| **Logged in, but a staff/admin page returns 403** | Working exactly as designed — the account's role does not permit it | Log in as `staff` or `admin`. Roles are checked server-side on every request, not by hiding navbar links |
| **`/recommendations/explain` takes ~20 seconds the first time** | The model was not warmed up, so the first request pays the load cost | Set `LLM_WARMUP=true` (already the default in development) and wait for `Local LLM warmed up in ...s` before using the page (§12.4) |

---

## Where to go next

* **`docs/TECHNICAL_HANDOFF.md`** — how the whole system works: architecture, routes,
  services, the recommendation engine, the local AI, the preference safeguard.
* **`HANDOFF.md`** — the condensed one-page setup summary.
* **`README.md`** — project overview and rationale.
* **`docs/DATABASE.md`** — deeper schema notes and design reasoning.
* **`docs/SECURITY.md`** — the full security model, including §17's list of what is
  still outstanding before a real deployment.
* **`docs/PROJECT_PROGRESS.md`** — the authoritative record of what has actually been
  built, milestone by milestone.

---

*Verified against commit `912150a` on 2026-08-20. Database: MySQL 8.0.46. Migration
head: `2d9f3b20045f`. Test suite: 420 passed, 0 failed, 0 skipped.*
