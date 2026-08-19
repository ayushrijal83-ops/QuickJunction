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

**The production model is v4** — `models/qwen3-0.6b-quickjunction-lora-v4`.
It is the **final selected adapter**; no further training is planned, and
none is required to set the project up.

v4 ships with a **deterministic preference safeguard**
(`app/services/local_llm.py`). Before an explanation is shown it is checked
against the customer's actual stored preferences, and any sentence that
credits them with a preference they never set is discarded and replaced by a
grounded, deterministic one. Promotion of v4 depended on that guard: the
adapter alone scored 23/25 on the held-out production evaluation and 25/25
with the guard applied. The two ship together.

Full detail: [`docs/AI.md`](docs/AI.md).

## 7. Dataset and LoRA fine-tuning

- **Hand-authored** for this project — not scraped, not downloaded, not
  generated by another language model.
- **v4 (production)**: 240 examples, 15 categories, 195 train / 45 validation.
- **v1** (60), **v2** (150) and **v3** (200) are preserved as the record of
  the earlier training runs and comparisons.
- Deterministic, content-addressed split (SHA-256, no RNG); the builder
  rejects duplicates and train/validation overlap.
- All four adapters are kept on disk. **Only v4 is used in production**;
  v1–v3 are historical/reproducibility artifacts and are not needed to run
  the application.

**Setting the project up requires no training.** The v4 adapter is a finished
artifact that is copied in, not rebuilt. `training/` is retained so the work
is reproducible and auditable, not because a new machine has to run it.

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

### Model files are not in Git

The model weights are **deliberately excluded from version control** — they
are roughly **1.4 GB**, far beyond what belongs in a Git repository, so
`models/` is listed in `.gitignore`.

**Cloning this repository does not install the AI model.** The `models/`
directory must be **copied separately** by the project owner. Until it is
present, every deterministic feature works normally and the explanation panel
shows "AI explanation temporarily unavailable".

Required layout:

```text
models/
├── Qwen3-0.6B-Base/                      # foundation weights (~1.2 GB) - REQUIRED
└── qwen3-0.6b-quickjunction-lora-v4/     # production LoRA adapter (~35 MB) - REQUIRED
```

- **`Qwen3-0.6B-Base/`** — the foundation model. The application loads it
  locally and applies the adapter on top of it.
- **`qwen3-0.6b-quickjunction-lora-v4/`** — the production adapter. Required
  for the production AI behaviour that was evaluated and approved.
- Everything runs **fully offline**: `HF_HUB_OFFLINE=1` and
  `local_files_only=True` are set before `transformers` is imported, so no
  download is attempted at any point.
- `models/qwen3-0.6b-quickjunction-lora`, `-v2` and `-v3` may also be present.
  They are **historical/reproducibility artifacts only** and are not required
  for normal production use.

**Acquisition:** this repository does not document an official download
source, and none is invented here. **The model package must be transferred
separately by the project owner** (for example on external storage or an
internal file share). Verify a copied installation with:

```bash
python scripts/smoke_local_model.py
```

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
| `LLM_ADAPTER_PATH` | `models/qwen3-0.6b-quickjunction-lora-v4` | LoRA adapter (production) |
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

## 15. New machine / handoff setup

The complete sequence for a developer setting Quick Junction up from scratch.
Steps 1-4 and 6-9 are **required**; the rest depend on whether you need the AI
explanation feature.

**1. Copy or clone the repository.**

```bash
git clone <repository-url> quick-junction
cd quick-junction
```

**2. Install Python 3.10 or newer.** Verify with `python --version`. The
project is developed and tested on 3.10.11.

**3. Create and activate a virtual environment.**

```bash
python -m venv .venv
.venv\Scriptsctivate          # Windows
source .venv/bin/activate       # Linux / macOS
```

**4. Install the web dependencies** (required).

```bash
pip install -r requirements.txt
```

**5. Install the AI dependencies** — only if you need explanations.

```bash
pip install -r requirements-ai.txt
```

Skipping this is fully supported: the application runs and every
deterministic feature works, with the explanation panel showing
"temporarily unavailable".

**6. Install and start MySQL 8.** Then create the schema and a user:

```sql
CREATE DATABASE quick_junction CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'qj_user'@'127.0.0.1' IDENTIFIED BY '<choose-a-strong-password>';
GRANT ALL PRIVILEGES ON quick_junction.* TO 'qj_user'@'127.0.0.1';
FLUSH PRIVILEGES;
```

**7. Create `.env` from the template.**

```bash
copy .env.example .env          # Windows
cp .env.example .env            # Linux / macOS
```

**8. Configure `DATABASE_URL` and `SECRET_KEY`** in `.env`:

```ini
DATABASE_URL=mysql+pymysql://qj_user:<your-password>@127.0.0.1:3306/quick_junction
SECRET_KEY=<64-char random string>
```

Generate a key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

**9. Configure the model paths** (only if using AI). **Usually nothing to do**
— the built-in defaults already point at `models/Qwen3-0.6B-Base` and
`models/qwen3-0.6b-quickjunction-lora-v4` relative to the repository, so
leave `LLM_MODEL_PATH` and `LLM_ADAPTER_PATH` commented out in `.env`.

Only set them if your weights live elsewhere. If you do, prefer an **absolute
path**: a relative value is resolved against the current working directory,
not the repository root, so it breaks if the application is started from
another directory.

**10. Copy the `models/` directory separately** — see
"Model files are not in Git" in §9. It is not in the repository and must be
transferred by the project owner. Verify with:

```bash
python scripts/smoke_local_model.py
```

**11. Apply the database migrations.**

```bash
flask --app run.py db upgrade
```

**12. Seed demonstration data** (optional, development only).

```bash
python scripts/seed_demo.py
```

**13. Start the application.**

```bash
python run.py
```

It serves on `http://127.0.0.1:5000` by default. `run.py` is a development
entry point; production uses a WSGI server:

```bash
gunicorn "app:create_app('production')"
```

**14. Run the test suite.**

```bash
pytest -q
```

All tests should pass. They use in-memory SQLite and never touch MySQL or the
model, so this step works before steps 6-10 are done.

### Required vs optional

| Component | Status | Notes |
| --- | --- | --- |
| `app/`, `config.py`, `run.py`, `migrations/` | **required** | the application itself |
| `requirements.txt` | **required** | web dependencies |
| MySQL 8 + `.env` | **required** | no default `DATABASE_URL`; fails fast if unset |
| `requirements-ai.txt` | optional | only for AI explanations |
| `models/Qwen3-0.6B-Base/` | optional* | *required for AI explanations |
| `models/qwen3-0.6b-quickjunction-lora-v4/` | optional* | *required for production AI behaviour |
| `models/…-lora`, `-v2`, `-v3` | not needed | historical artifacts |
| `data/`, `training/` | not needed at runtime | dataset provenance and reproducibility |
| `scripts/seed_demo.py` | optional | demo data, development only |
| `.venv/`, `logs/`, `__pycache__/`, `.pytest_cache/` | excluded | local development artifacts, git-ignored |

A short version of this sequence lives in [`HANDOFF.md`](HANDOFF.md).

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

Feature-complete and released. The AI line of work is finished: **v4 is the
final production adapter**, evaluated and hardened, and no further training is
planned.

Current state, verified results and known limitations:
[`docs/PROJECT_PROGRESS.md`](docs/PROJECT_PROGRESS.md).
Handoff checklist: [`HANDOFF.md`](HANDOFF.md).
