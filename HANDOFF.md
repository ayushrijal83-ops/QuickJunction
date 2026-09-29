# Quick Junction — Developer Handoff

Everything needed to get Quick Junction running on a new machine. For the
reasoning behind any of it, see [`README.md`](README.md) and [`docs/`](docs/).

---

## Project

Quick Junction is a Flask restaurant-management application: menu browsing,
cart and checkout, staff order fulfilment, a deterministic recommendation
engine, and a locally fine-tuned language model that explains the top
recommendation in plain English.

**No customer data leaves the deployment.** The model runs on the same machine
as the application — no external AI API, no API key, no network call at
inference time.

**Production model: `models/qwen3-0.6b-quickjunction-lora-v4`** (v4). This is
the final selected adapter; no further training is planned or required.

| Directory | Contains |
| --- | --- |
| `app/` | the application — routes, services, models, templates |
| `config.py`, `run.py` | configuration and the development entry point |
| `migrations/` | Alembic migrations (5 versions, single head) |
| `models/` | **not in Git** — model weights, copied separately |
| `data/` | hand-authored datasets v1–v4 and their processed splits |
| `training/` | training and evaluation harnesses, recorded results |
| `scripts/` | demo seeding, dataset builder, model smoke test |
| `tests/` | 378 tests |
| `docs/` | architecture, database, security, AI, progress |

---

## Requirements

| | |
| --- | --- |
| OS | Windows and Linux both supported. Developed and verified on Windows 11; nothing in the code is OS-specific |
| Python | **3.10 or newer** (developed on 3.10.11) |
| MySQL | **8.0+** (verified against 8.0.46) |
| Disk | ~1.5 GB for the model weights, plus dependencies |
| GPU | **Not required.** Everything is CPU-only by design |

Dependencies are split in two, and pinned:

- `requirements.txt` — the web application. **Required.**
- `requirements-ai.txt` — local model inference. **Optional**; without it the
  application runs fully and only the AI explanation is unavailable.

---

## Setup

**1. Copy or clone the repository.**

```bash
git clone <repository-url> quick-junction
cd quick-junction
```

**2. Create and activate a virtual environment.**

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS
```

**3. Install dependencies.**

```bash
pip install -r requirements.txt          # required
pip install -r requirements-ai.txt       # optional: AI explanations
```

**4. Create `.env` from the template.**

```bash
copy .env.example .env          # Windows
cp .env.example .env            # Linux / macOS
```

Then set two values in it:

```ini
DATABASE_URL=mysql+pymysql://qj_user:<your-password>@127.0.0.1:3306/quick_junction
SECRET_KEY=<paste a 64-char random string>
```

Generate the key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

The model path variables can stay commented out — the built-in defaults are
already correct for the standard layout.

**5. Create the MySQL database.**

```sql
CREATE DATABASE quick_junction CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'qj_user'@'127.0.0.1' IDENTIFIED BY '<choose-a-strong-password>';
GRANT ALL PRIVILEGES ON quick_junction.* TO 'qj_user'@'127.0.0.1';
FLUSH PRIVILEGES;
```

**6. Apply migrations.**

```bash
flask --app run.py db upgrade
```

**7. Install the model files** — see the next section.

**8. Seed demo data** (optional, development only).

```bash
python scripts/seed_demo.py
```

---

## Model layout

The weights are roughly **1.4 GB** and are deliberately **not stored in Git**
(`models/` is git-ignored). **Cloning the repository does not install them.**
The directory must be **transferred separately by the project owner** — this
repository documents no download source and none should be assumed.

```text
models/
├── Qwen3-0.6B-Base/                      # foundation weights (~1.2 GB) - REQUIRED
└── qwen3-0.6b-quickjunction-lora-v4/     # production adapter (~35 MB) - REQUIRED
```

`models/qwen3-0.6b-quickjunction-lora`, `-v2` and `-v3` may also be present.
They are **historical artifacts** kept for reproducibility and are **not
needed** to run the application.

Verify an installation:

```bash
python scripts/smoke_local_model.py
```

It loads the base model and generates once. It reads `LLM_MODEL_PATH` if set,
otherwise `models/Qwen3-0.6B-Base` relative to the repository root.

---

## Run

```bash
python run.py
```

Serves on `http://127.0.0.1:5000` (override with `FLASK_RUN_HOST` /
`FLASK_RUN_PORT`).

`run.py` is a **development** entry point. Production uses a WSGI server:

```bash
gunicorn "app:create_app('production')"
```

Production configuration is validated on startup and **refuses to run** with a
weak or placeholder `SECRET_KEY`, with debug enabled, or against SQLite.

---

## Test

```bash
pytest -q
```

Expect **378 passing**. The suite uses in-memory SQLite and never loads the
model, so it works before MySQL or `models/` are set up — which makes it a
good first check that the install is sound.

---

## AI behaviour

- **v4 LoRA adapter.** Qwen3-0.6B-Base is loaded locally and the v4 LoRA
  adapter is applied on top. The model *explains* a recommendation the
  deterministic engine already made; it never selects items, prices anything,
  or judges dietary safety.
- **Offline.** `HF_HUB_OFFLINE=1` and `local_files_only=True` are set before
  `transformers` is imported. No download is attempted, ever.
- **Production safeguard.** Every generated explanation is checked against the
  customer's actual stored preferences before display. A sentence that credits
  them with a preference they never set — or that leaks the internal
  "not set" placeholder — is **discarded and replaced** by a deterministic
  explanation built from the same facts. This is not optional: v4 scored 23/25
  without it and 25/25 with it.
- **Graceful degradation.** If `models/` is missing, the adapter fails to load,
  or `requirements-ai.txt` was never installed, the application logs a warning
  and continues. The explanation panel shows "AI explanation temporarily
  unavailable" and every other feature works normally. **A missing model is
  never a crash.**
- **Speed.** CPU generation takes a few seconds, which is why explanations live
  on their own page (`/recommendations/explain`) and `/recommendations` never
  invokes the model.

---

## Security

- **Never commit `.env`.** It holds the real `SECRET_KEY` and database
  password. It is git-ignored; keep it that way.
- **Never share production secrets** through the repository, an issue tracker,
  or a chat message. If a secret is ever committed, treat it as compromised:
  rotate it first, clean history second.
- **Change the demo credentials before any real deployment.** The accounts
  created by `scripts/seed_demo.py` use a password that is written in plain
  text in that script and is therefore public.
- **Do not use the demo password in production**, and do not leave demo
  accounts in a production database at all.
- Generate a fresh `SECRET_KEY` per environment. Production startup rejects
  keys shorter than 32 characters and known placeholder values.

---

## Accounts and sign-in

* **Customers** register at `/register` (signed in automatically) and sign in at
  `/login`. Taste preferences and food suggestions exist **only** in the customer
  portal; staff and admin accounts get 403 on those pages.
* **Staff** request an account at `/register/staff` and sign in at `/login/staff`. A new
  staff account is **pending** and cannot sign in until an admin approves it.
* **Admins** sign in at `/login/admin`. There is no public admin sign-up; admin
  accounts are provisioned by the operator (e.g. `scripts/seed_demo.py`).
* **Approving staff:** Admin dashboard → *Staff accounts* (`/admin/staff`) → *Approve*.
  *Revoke access* signs that staff member out on their next request.
* Choosing a portal never grants a role; the role stored on the account decides.

Details: `docs/PHASE3_PRODUCT_IMPROVEMENTS.md`.

## Troubleshooting

**AI explanation shows "temporarily unavailable"**
Check `models/` exists with both `Qwen3-0.6B-Base/` and
`qwen3-0.6b-quickjunction-lora-v4/`, and that `requirements-ai.txt` is
installed. Run `python scripts/smoke_local_model.py` to isolate it. Application
logs record which of the two failed.

**Database connection failure on startup**
Check `DATABASE_URL` in `.env`. Confirm MySQL is running, the schema exists,
and the user has privileges. Percent-encode any special characters in the
password. There is no default — an unset `DATABASE_URL` fails immediately, by
design.

**"Table doesn't exist" / schema errors**
Migrations have not been applied. Run `flask --app run.py db upgrade`. Confirm
with `flask --app run.py db current`; the head is `7c4e1a9b52d3`.

**Model loading failure**
Verify both paths: the base model *and* the v4 adapter. If `LLM_MODEL_PATH` or
`LLM_ADAPTER_PATH` are set in `.env`, prefer absolute paths — a relative value
is resolved against the current working directory, not the repository root. If
the adapter alone fails, the application falls back to the base model and logs
"Could not load LoRA adapter"; output quality will be noticeably worse.

**Dependency / import failures**
Check `python --version` is 3.10+. Install **both** requirements files if you
need AI. The pins are deliberate and mutually verified — in particular numpy
is pinned once, in `requirements.txt`, and `requirements-ai.txt` intentionally
does not re-pin it. If `transformers` or `peft` raise an unrelated TensorFlow
`ImportError`, set `USE_TF=0` before importing; the application already does
this internally.
