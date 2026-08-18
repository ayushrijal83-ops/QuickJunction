# Quick Junction

A restaurant-management web application: menu browsing, cart and checkout,
staff order fulfilment, a deterministic recommendation engine, and a locally
fine-tuned language model that explains recommendations in plain English.

**No customer data ever leaves the deployment.** The language model runs on
the same machine as the application; there is no external AI API, no API key,
and no network call at inference time.

---

## 1. What Quick Junction is

A final-year-project-scale but production-shaped Flask application covering
the full restaurant ordering lifecycle:

```
browse menu → set preferences → get recommendations → cart → checkout
                                        ↓
                            staff fulfil the order
                                        ↓
                        customer watches the status change
```

## 2. Main features

| Area | What it does |
| --- | --- |
| **Accounts** | Registration, login, logout; three roles (customer / staff / admin) |
| **Menu** | Public browsing by category, with cuisine, dietary and spice attributes |
| **Menu management** | Admin creates/edits categories and items; staff may view |
| **Cart** | Session-based; stores only item ids and quantities, never a price |
| **Checkout** | Every price recalculated server-side inside one transaction |
| **Orders** | Customer order history with price snapshots frozen at purchase |
| **Staff queue** | All orders, with a validated status workflow |
| **Preferences** | Diet, cuisine and spice level, used to personalise |
| **Recommendations** | Deterministic TF-IDF + cosine similarity, dietary-safe |
| **AI explanation** | Locally fine-tuned Qwen adapter explains the top pick |
| **Audit trail** | Security and business events in `audit_logs` |

## 3. Architecture

```
run.py / WSGI
    │
create_app(config)                app/__init__.py
    ├── config + validate          config.py
    ├── logging                    app/utils/logging.py
    ├── db / migrate / csrf        app/extensions.py
    ├── error handlers             app/utils/errors.py
    └── blueprints                 app/routes/
                                        │
                    routes → services → models → MySQL
```

Dependencies point one way: **routes → services → models**. Services never
import `flask.request`, so every business rule is callable from a script, a
job or a test.

| Layer | Rule |
| --- | --- |
| `app/routes/` | Parse and validate the request, call a service, render. No business rules. |
| `app/services/` | Business rules and transactions. No HTTP. |
| `app/models/` | SQLAlchemy tables. No HTTP, no business rules. |
| `app/utils/` | Logging, errors, auth helpers. Imports nothing above it. |

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## 4. Security architecture

Full detail: [`docs/SECURITY.md`](docs/SECURITY.md). The load-bearing rules:

- **Passwords** are Argon2id (`argon2-cffi`), never reversible, never logged,
  never returned in a response.
- **Authorization is server-side only.** The role is re-read from the
  database on every request; no header, cookie field or form value can grant
  access. Forged `X-Role` headers are ignored.
- **CSRF** protects every state-changing form (Flask-WTF). A missing or
  forged token is rejected with `400` before the view runs.
- **IDOR protection**: order ownership is filtered *inside the SQL query*, so
  another customer's order returns `404` — never `403`, which would itself
  confirm the id exists.
- **Money is `Decimal`/`DECIMAL(10,2)` end to end.** No float touches a price.
- **Prices are never accepted from the client.** The cart holds only ids and
  quantities; checkout recalculates everything from the live menu.
- **Dietary restrictions are a hard filter** applied before ranking, so
  neither the similarity score nor order history can surface a restricted
  item.
- **The LLM has no authority**: it is given no price, no id and no
  availability flag, and nothing reads its output back.

## 5. Recommendation engine

`app/services/recommendations.py` — deterministic, in-process, no network.

1. Candidates = available items in active categories.
2. **Dietary hard filter** applied before any scoring.
3. Each item becomes a text document (name, description, category, cuisine,
   `spice_*`, `diet_*`, ingredients). Price and database id are deliberately
   excluded.
4. Preferences become a query document in the same vocabulary.
5. TF-IDF + cosine similarity; ties broken on name so output is stable.
6. Completed orders contribute at half weight — never enough to override a
   stated dietary restriction.

Same inputs always produce the same ranking.

## 6. Local AI (Qwen)

**We did not train a large language model from scratch.**

We used **Qwen3-0.6B-Base** as the foundation model, wrote a project-specific
instruction dataset by hand, fine-tuned it locally with **LoRA**
(parameter-efficient — 0.84 % of parameters trained), evaluated the resulting
adapter against held-out scenarios, and integrated the adapter into Quick
Junction as an **explanation-only** component.

The model explains a recommendation the deterministic engine already made. It
may not select items, set prices, judge dietary safety, or modify anything.
If it is unavailable the page still works and shows the deterministic facts
with a short notice.

Full detail: [`docs/AI.md`](docs/AI.md).

## 7. Dataset and LoRA fine-tuning

- **Hand-authored** for this project — not scraped, not downloaded, not
  generated by another language model.
- **v2 (current)**: 150 examples, 15 categories, 120 train / 30 validation.
- **v1** (60 examples) is preserved as the record of the first training run.
- Deterministic, content-addressed split (SHA-256, no RNG); the builder
  rejects duplicates and train/validation overlap.
- Both adapters are kept: `models/qwen3-0.6b-quickjunction-lora` (v1) and
  `models/qwen3-0.6b-quickjunction-lora-v2` (v2, in use).

Provenance and limitations: [`data/README.md`](data/README.md).

## 8. Database

MySQL 8, SQLAlchemy 2 + Alembic. Nine tables: `users`, `audit_logs`,
`categories`, `menu_items`, `ingredients`, `menu_item_ingredients`,
`customer_preferences`, `orders`, `order_items`.

Verified against a live MySQL 8.0.46 server: all 15 `CHECK` constraints
present **and enforced**, every money column `decimal(10,2)` with no
float/double anywhere, all 8 foreign keys with the intended `ON DELETE`
rules, all tables InnoDB.

Schema detail: [`docs/DATABASE.md`](docs/DATABASE.md).

---

## 9. Install

Requires **Python 3.10+** and a reachable **MySQL 8** server.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS

pip install -r requirements.txt          # web application
pip install -r requirements-ai.txt       # local model (optional, see below)
```

`requirements-ai.txt` is optional. Without it the whole application works;
only the AI explanation degrades to "temporarily unavailable".

The model weights are **not** in the repository. Place
`Qwen3-0.6B-Base` under `models/` (git-ignored) to enable explanations.

## 10. Configure `.env`

```bash
cp .env.example .env            # copy .env.example .env   on Windows
python -c "import secrets; print(secrets.token_urlsafe(64))"   # -> SECRET_KEY
```

Then edit `.env`:

```ini
APP_ENV=development
SECRET_KEY=<the generated value>
DATABASE_URL=mysql+pymysql://<user>:<password>@127.0.0.1:3306/quick_junction
```

**`.env` is git-ignored and must never be committed.** This repository
contains no real secret.

Create the database once:

```sql
CREATE DATABASE quick_junction CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'qj_user'@'localhost' IDENTIFIED BY '<strong-password>';
GRANT ALL PRIVILEGES ON quick_junction.* TO 'qj_user'@'localhost';
```

Optional AI settings (all have working defaults):

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_ENABLED` | `true` | master switch for explanations |
| `LLM_MODEL_PATH` | `models/Qwen3-0.6B-Base` | base weights |
| `LLM_ADAPTER_PATH` | `models/qwen3-0.6b-quickjunction-lora-v2` | LoRA adapter |
| `LLM_MAX_NEW_TOKENS` | `48` | bounds explanation latency |

## 11. Run migrations

```bash
flask db upgrade                            # apply
flask db current                            # should print 38297b707b89 (head)
flask db downgrade                          # roll back one
```

Always read a generated migration before applying it — Alembic autogenerate
guesses, and its guesses can drop columns.

## 12. Run tests

```bash
pytest
```

**221 tests.** The suite uses in-memory SQLite (and a temporary on-disk
SQLite for the migration tests), so it needs no MySQL server and touches no
real data. The language model is disabled in the testing configuration and is
never loaded by the suite.

## 13. Run the application

```bash
python scripts/seed_demo.py     # optional: demo menu + accounts
python run.py                   # http://127.0.0.1:5000
```

Production is served by a WSGI server, never `run.py`:

```bash
gunicorn "app:create_app('production')"
```

## 14. Demo setup

`python scripts/seed_demo.py` is idempotent and creates a 10-item menu (one
deliberately unavailable, to show availability filtering) plus three accounts:

| Username | Password | Role |
| --- | --- | --- |
| `customer` | `demo-password-1` | customer |
| `staff` | `demo-password-1` | staff |
| `admin` | `demo-password-1` | admin |

These are **development-only placeholders**. The seeder refuses to run against
a production configuration, and these passwords could not satisfy the
production password policy.

A five-minute demo path:

1. `/` → browse the menu
2. Register, or log in as `customer`
3. **My preferences** → vegetarian / Indian / hot → save
4. **Recommended** → note that meat dishes are absent entirely
5. **Why the top pick?** → deterministic facts beside the AI explanation
6. Add to cart → checkout → order appears as `PENDING`
7. Log in as `staff` → **Order queue** → walk `PENDING → … → COMPLETED`
8. Back as `customer` → the status is updated

---

## Security rules (non-negotiable)

1. **No secret is ever written into source.** Everything comes from the
   environment; `.env.example` holds placeholders only.
2. **Never commit `.env`.** Check `git status` before every commit.
3. **Debug is never enabled outside development.** The production config
   refuses to start with debug on, a weak `SECRET_KEY`, or a non-MySQL URL.
4. **Errors never leak internals.** Clients get a generic message; tracebacks
   go to the log.
5. **Validate every input at the trust boundary**, and again in the service.
6. **Never log a credential.** Log identifiers.
7. **Authorization is server-side only.**
8. **Every state-changing form carries a CSRF token.**
9. **Login errors never distinguish "no such user" from "wrong password."**
10. **Money is `Decimal`, never `float`**, and a price is never accepted from
    a client.
11. **Allow-listed fields are enforced at the database**, not just the form.
12. **Row ownership is checked in the query**, not after fetching.
13. **The LLM is never an authority** on price, availability, diet, identity
    or database state.

If a secret is ever committed, treat it as compromised: rotate first, clean
history second.

## Project status

Milestones 01–08 complete. Current state, verified results and known
limitations: [`docs/PROJECT_PROGRESS.md`](docs/PROJECT_PROGRESS.md).
