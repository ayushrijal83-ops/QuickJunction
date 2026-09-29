# Quick Junction — Complete Technical Architecture & Developer Handover

**Audience:** a developer who will maintain, extend, or take ownership of Quick
Junction.

**Goal:** after reading this, you should be able to trace a request from the browser to
the database and back — including through the recommendation engine and the local
language model — **without opening the source code first**. When you do open the source,
you should know exactly which file to open.

> **Everything here was verified against the code in this repository at commit
> `912150a` on 2026-08-20.** Where a measurement is quoted, the document says how and
> when it was measured. Where this document disagrees with an older document in the
> repository, the disagreement is called out explicitly rather than papered over.

---

## Table of contents

| § | Section |
| --- | --- |
| 1 | [Project overview](#1--project-overview) |
| 2 | [Repository structure](#2--repository-structure) |
| 3 | [The Flask application factory](#3--the-flask-application-factory) |
| 4 | [Blueprints and routes](#4--blueprints-and-routes) |
| 5 | [Authentication and authorization](#5--authentication-and-authorization) |
| 6 | [Frontend architecture](#6--frontend-architecture) |
| 7 | [Frontend → backend communication](#7--frontend--backend-communication) |
| 8 | [The Python backend](#8--the-python-backend) |
| 9 | [SQLAlchemy and the database layer](#9--sqlalchemy-and-the-database-layer) |
| 10 | [The recommendation engine](#10--the-recommendation-engine) |
| 11 | [Local AI architecture](#11--local-ai-architecture) |
| 12 | [Model files](#12--model-files) |
| 13 | [How Python loads the AI](#13--how-python-loads-the-ai) |
| 14 | [AI warm-up](#14--ai-warm-up) |
| 15 | [ExplanationRequest](#15--explanationrequest) |
| 16 | [The preference safeguard](#16--the-preference-safeguard) |
| 17 | [Recommendation vs AI — the comparison](#17--recommendation-vs-ai--the-comparison) |
| 18 | [AI failure and no-model behaviour](#18--ai-failure-and-no-model-behaviour) |
| 19 | [Dataset and training history](#19--dataset-and-training-history) |
| 20 | [Configuration](#20--configuration) |
| 21 | [Logging and errors](#21--logging-and-errors) |
| 22 | [Security architecture](#22--security-architecture) |
| 23 | [Testing architecture](#23--testing-architecture) |
| 24 | [Important files map](#24--important-files-map) |
| 25 | ["If you need to change X, go here"](#25--if-you-need-to-change-x-go-here) |
| 26 | [Complete request lifecycle](#26--complete-request-lifecycle) |
| 27 | [Complete startup lifecycle](#27--complete-startup-lifecycle) |
| 28 | [Handover rules](#28--handover-rules) |
| 29 | [Final handover checklist](#29--final-handover-checklist) |

For installation, see **`docs/MYSQL_SETUP_HANDOFF.md`**. This document assumes the
application already runs.

---

## 1 — Project overview

### 1.1 What Quick Junction is

A restaurant management and ordering web application. A customer states what they eat,
gets a ranked list of dishes filtered and scored against those preferences, and can
order. Staff work the resulting orders through a fixed status workflow. An
administrator manages the menu that all of it draws on.

The distinctive part is the last mile: the top recommendation is accompanied by a
**plain-English explanation written by a language model that runs on the same machine**
— no cloud API, no API key, no network call.

### 1.2 What problem it solves

A long menu makes the customer do the filtering. Someone who is vegan, or cannot take
heat, has to read every item to find the few they can order. Quick Junction inverts
that: state three preferences once, and the system removes what your diet excludes,
ranks what remains, and tells you *why* the top result was chosen — with the underlying
facts shown next to the claim so you can check it.

### 1.3 Who uses it

| Role | Can do |
| --- | --- |
| **Customer** | Browse the menu, set taste preferences, see recommendations and their AI explanation, use the cart, check out, view their own order history |
| **Staff** | Everything a customer can, plus the order queue: view every order and advance it through the status workflow. Can also view the admin menu *list* (read-only) |
| **Admin** | Everything staff can, plus full create/edit of categories and menu items |

Roles are a single column on `users`, enforced server-side by decorators on each route.

### 1.4 Major features

* Public landing page and menu, browsable without an account
* Registration and login with Argon2id hashing, with **automatic sign-in on
  registration**
* Three-dimension customer preferences (cuisine, dietary type, spice level), each
  independently optional
* A **deterministic** recommendation engine — TF-IDF plus cosine similarity — with the
  dietary restriction applied as a hard filter *before* any scoring
* A locally fine-tuned language model that explains the top recommendation
* A deterministic **preference safeguard** that rejects and replaces explanations
  making claims the stored preferences do not support
* Session-based cart, server-side priced checkout, order history
* Staff order queue with an allow-listed status state machine
* Admin CRUD for categories and menu items
* CSRF protection, role-based authorization, IDOR-safe ownership scoping, server-side
  session revocation, an append-only audit trail
* Graceful degradation: **the application is fully usable with no model files at all**

### 1.5 Technology stack

| Layer | Technology |
| --- | --- |
| Web framework | Flask 3.1.3 |
| Templating | Jinja2 (bundled with Flask), server-side rendered |
| CSS | Bootstrap 5, **vendored locally** under `app/static/vendor/` — no CDN |
| JavaScript | ~60 lines of vanilla JS in `app/static/js/loading.js`, plus Bootstrap's bundle. No build step, no framework |
| ORM | SQLAlchemy 2.0.52 via Flask-SQLAlchemy 3.1.1 |
| Migrations | Alembic 1.19.1 via Flask-Migrate 4.1.0 |
| Database | MySQL 8.0+ (verified 8.0.46) via PyMySQL 1.2.0 |
| Forms / CSRF | Flask-WTF 1.3.0, WTForms 3.2.2 |
| Password hashing | argon2-cffi 25.1.0 (Argon2id) |
| Recommendations | scikit-learn 1.7.2 (`TfidfVectorizer`, `cosine_similarity`) |
| Local LLM | PyTorch (CPU) + Transformers + PEFT, running Qwen3-0.6B-Base with a LoRA adapter |
| Tests | pytest 9.1.1, in-memory SQLite |

### 1.6 Architecture style

A **layered, server-rendered monolith**. One process, no client-side framework, no
microservices, no message queue, no background job runner. Every page is rendered on the
server and returned as complete HTML.

```text
Browser
   ↓   HTTP request
Flask Routes            app/routes/      parse, authorize, validate, respond
   ↓   plain Python arguments
Services                app/services/    business rules, transactions
   ↓   ORM calls
SQLAlchemy Models       app/models/      table definitions, relationships
   ↓   SQL over TCP
MySQL
```

The rule each layer obeys:

* **Routes** touch `flask.request` and `flask.session`. They never contain business
  rules.
* **Services** never touch `flask.request` or `flask.session`. They receive plain
  arguments — including an already-authenticated `user_id` — and raise
  `ValidationError` subclasses on bad input.
* **Models** describe tables and relationships. They contain no query logic beyond
  relationship definitions.
* **Utilities** (`app/utils/`) are cross-cutting: authorization decorators, logging,
  formatting filters, the session cart, the rate limiter.

### 1.7 The AI request flow

```text
Recommendation Request  (GET /recommendations/explain)
        ↓
Preference Service              app/services/preferences.py
   get_preferences(user_id) → CustomerPreference | None
        ↓
Recommendation Engine           app/services/recommendations.py
   dietary hard filter → TF-IDF → cosine similarity → ranked list
        ↓
Recommended Menu Item           results[0] — a real MenuItem row
        ↓
ExplanationRequest              app/services/local_llm.py
   frozen dataclass: item facts + preferences (or None) + match label
   NO price. NO id. NO availability flag.
        ↓
Local Qwen Base Model           models/Qwen3-0.6B-Base
        +
Quick Junction V4 LoRA          models/qwen3-0.6b-quickjunction-lora-v4
   greedy generation, max 48 new tokens, CPU
        ↓
Preference Safeguard            apply_preference_safeguard()
   claim unsupported by stored preferences? → discard, replace
        ↓
Safe Explanation                a string, or None
        ↓
Browser                         app/templates/preferences/explain.html
   deterministic fact panel (authoritative) + AI panel (advisory)
```

**Read the shape of that diagram carefully.** The model sits near the *end*, and
everything before it is deterministic. The model does not choose the dish. It describes
a choice that has already been made and validated.

---

## 2 — Repository structure

```text
QuickJunction/
├── app/                    the application — runtime code
│   ├── __init__.py         create_app() — the application factory
│   ├── extensions.py       unbound db / migrate / csrf instances
│   ├── models/             SQLAlchemy table definitions
│   ├── routes/             Flask blueprints (the HTTP layer)
│   ├── services/           business logic
│   ├── utils/              cross-cutting helpers
│   ├── templates/          Jinja2 HTML
│   └── static/             CSS, JS, vendored Bootstrap
├── config.py               configuration classes + validation
├── run.py                  development entry point
├── migrations/             Alembic migrations (5 files, single head)
├── scripts/                seeding, dataset builder, model smoke test
├── training/               LoRA training and evaluation harnesses
├── data/                   hand-authored datasets v1–v4 and processed splits
├── models/                 NOT IN GIT — model weights, copied separately
├── tests/                  pytest suite (420 tests)
├── docs/                   architecture, database, security, AI, progress
├── logs/                   NOT IN GIT — app.log and security.log
├── requirements.txt        web application dependencies
├── requirements-ai.txt     local inference / training dependencies
├── .env                    NOT IN GIT — real secrets
├── .env.example            committed template, placeholders only
└── pytest.ini              testpaths, pythonpath, addopts = -q
```

### 2.1 Directory by directory

| Directory | Purpose | Category | Modify? |
| --- | --- | --- | --- |
| **`app/`** | The entire running application | **Production runtime** | Yes — this is where feature work happens |
| `app/models/` | Table definitions. 8 table modules plus `enums.py` | Production runtime | Yes, but **any change needs a migration** (§25) |
| `app/routes/` | 10 blueprint modules, plus `forms.py`. HTTP only | Production runtime | Yes — add routes here, not business rules |
| `app/services/` | 9 modules of business logic including the recommendation engine and the local LLM | Production runtime | Yes — this is where rules belong |
| `app/utils/` | Authorization, logging, formatting, session cart, rate limiter, error handling, security primitives | Production runtime | Carefully — these are used everywhere |
| `app/templates/` | 28 Jinja2 templates | Production runtime | Yes — all UI changes land here |
| `app/static/` | `css/app.css` (~40 lines), `js/loading.js` (~60 lines), `vendor/` (Bootstrap) | Production runtime | Yes for `css/` and `js/`. **Do not edit `vendor/`** — replace the file to upgrade Bootstrap |
| **`config.py`** | Three configuration classes and their validation | Production runtime | Rarely, and read §20 first |
| **`run.py`** | Development entry point. ~10 lines | Production runtime | Almost never |
| **`migrations/`** | Alembic versions | Production runtime | **Add** files via `flask db migrate`. **Never delete** existing ones |
| **`scripts/`** | `seed_demo.py`, `build_dataset.py`, `smoke_local_model.py` | Tooling | `seed_demo.py` yes. The other two are frozen with the AI work |
| **`training/`** | `train_lora.py`, `evaluate.py`, `evaluate_production.py`, `eval_scenarios_production.py` | Training infrastructure — **never imported by the application** | **No.** Training is complete and frozen (§19) |
| **`data/`** | `raw/` — four hand-authored JSONL seed files. `processed/` — deterministic train/validation splits | Training data (committed) | **No.** Frozen at v4 |
| **`models/`** | Base weights + LoRA adapters | Runtime artifacts, git-ignored | **No.** Never regenerate or edit (§12) |
| **`tests/`** | 16 test modules | Test infrastructure | Yes — add tests. **Never weaken existing ones** |
| **`docs/`** | This file, plus `ARCHITECTURE.md`, `DATABASE.md`, `SECURITY.md`, `AI.md`, `PROJECT_PROGRESS.md`, `MYSQL_SETUP_HANDOFF.md` | Documentation | Yes — update after every milestone |
| **`logs/`** | Rotating `app.log` and `security.log` | Runtime output, git-ignored | No — generated |

### 2.2 The most important distinction in this repository

```text
RUNTIME  — imported when the application runs
   app/  config.py  run.py  migrations/

TRAINING — never imported by the application
   training/  data/  scripts/build_dataset.py

ARTIFACT — loaded at runtime, produced by training, not in Git
   models/
```

`training/` and `data/` are **historical evidence** of how the production adapter was
produced. Deleting them would not stop the application. They exist so the work is
reproducible and defensible. `models/` is the only bridge between the two worlds: the
application loads it, training produced it.

---

## 3 — The Flask application factory

`app/__init__.py` — 78 lines, one public function.

### 3.1 Why a factory instead of a module-level `app`

A module-level `app = Flask(__name__)` binds one configuration for the life of the
process. `create_app(config_name)` lets the test suite build a fresh application with
`TestingConfig` for each test, and lets `run.py` and a WSGI server build different ones
from the same code. It is what makes `create_app("testing")` in `tests/conftest.py`
possible.

### 3.2 The startup sequence, in order

The order is not arbitrary — the docstring in the file says *"Assembly order matters:
configuration is resolved and validated before any extension or handler is attached, so
a bad environment fails at startup rather than at the first request."*

1. **Resolve the configuration class.**
   `get_config(config_name)` in `config.py` maps `"development"` / `"testing"` /
   `"production"` to a class, falling back to the `APP_ENV` environment variable and
   then to `"development"`. An unknown name raises `ConfigError` immediately.

2. **Create the Flask object and load the config.**
   ```python
   app = Flask(__name__, instance_relative_config=True)
   app.config.from_object(config_class)
   ```

3. **Validate the configuration — `config_class.validate()`.**
   Fails fast if `SECRET_KEY` or `SQLALCHEMY_DATABASE_URI` is missing. In production it
   additionally rejects a weak or placeholder `SECRET_KEY`, debug/testing mode, and a
   SQLite DSN. **This runs before anything else is attached**, so a misconfigured
   deployment cannot half-start.

4. **Configure logging — `configure_logging(app)`.**
   Attaches a stream handler and, when `LOG_TO_FILE` is on, rotating file handlers for
   `logs/app.log` and `logs/security.log`. Emits
   `"Logging configured for <env> environment"` — the first line you see on startup.

5. **Initialise extensions.**
   ```python
   db.init_app(app)        # Flask-SQLAlchemy
   migrate.init_app(app, db)  # Flask-Migrate / Alembic
   csrf.init_app(app)      # Flask-WTF CSRFProtect — application-wide
   ```
   The instances themselves are created unbound in `app/extensions.py`, which is what
   lets models, routes and services `from app.extensions import db` without importing
   the factory and creating a circular import.

6. **Import the models package.**
   ```python
   from app import models  # noqa: F401
   ```
   Purely for the side effect of registering every model on `db.metadata`.
   **A model that is never imported is silently missing from every autogenerated
   migration.** `app/models/__init__.py` imports all nine modules for exactly this
   reason.

7. **Register error handlers — `register_error_handlers(app)`.**
   One handler for `HTTPException`, one catch-all for `Exception`. See §21.

8. **Register template filters — `register_filters(app)`.**
   Currently one: the `money` filter (§6.7).

9. **Register blueprints — `register_blueprints(app)`.**
   Ten blueprints, in the order listed in `app/routes/__init__.py`.

10. **Register the template context processor.**
    Injects two values into every template: `current_user` and `cart_count`.
    The comment is worth quoting because it prevents a common misreading:
    > *"Still just a display convenience — every route re-checks authorization itself."*
    >
    > *"`cart_count` … counts quantities in the signed session only — it touches no
    > database row and grants no authority, so a tampered cookie can at worst show a
    > wrong number on a badge."*

11. **Start the AI warm-up — `warm_up(app)`.**
    Returns immediately. Starts a daemon thread only if all its guards pass (§14).

12. **Return the application.** `run.py` then calls `app.run(...)`.

### 3.3 A note on `PROPAGATE_EXCEPTIONS`

`BaseConfig` sets `PROPAGATE_EXCEPTIONS = False` with the comment *"Never re-raise
handled exceptions to the client."* This is what guarantees the catch-all error
handler actually runs instead of Flask re-raising and leaking a traceback.

---

## 4 — Blueprints and routes

### 4.1 The ten blueprints

Registered in `app/routes/__init__.py` in this order:

| Blueprint | File | URL prefix | Purpose |
| --- | --- | --- | --- |
| `health` | `health.py` | — | Liveness probe |
| `main` | `main.py` | — | Public landing page |
| `auth` | `auth.py` | — | Register, log in, log out |
| `account` | `account.py` | `/account` | Account dashboard and role probes |
| `menu` | `menu.py` | `/menu` | Public menu browsing |
| `admin_menu` | `admin_menu.py` | `/admin` | Category and menu-item management |
| `cart` | `cart.py` | `/cart` | Session cart operations |
| `orders` | `orders.py` | — | Checkout and customer order history |
| `staff_orders` | `staff_orders.py` | `/staff` | Order queue and status workflow |
| `preferences` | `preferences.py` | — | Preferences, recommendations, AI explanation |

### 4.2 Every route

| Method | Path | Endpoint | Authorization |
| --- | --- | --- | --- |
| GET | `/health` | `health.health` | public |
| GET | `/` | `main.home` | public |
| GET/POST | `/register` | `auth.register` | public |
| GET/POST | `/register/staff` | `auth.register_staff` | public — creates a **pending** STAFF account |
| GET/POST | `/login` | `auth.login` | public — customer portal; any role may sign in |
| GET/POST | `/login/staff`, `/login/admin` | `auth.login` | public — refuses accounts of another role |
| POST | `/logout` | `auth.logout` | `@login_required` |
| GET | `/account/` | `account.index` | `@login_required` |
| GET | `/account/me` | `account.me` | `@login_required` |
| GET | `/account/staff` | `account.staff_only` | `@require_role(STAFF, ADMIN)` |
| GET | `/account/admin` | `account.admin_only` | `@require_role(ADMIN)` |
| GET | `/menu` | `menu.index` | public |
| GET | `/menu/<int:item_id>` | `menu.detail` | public |
| GET | `/cart` | `cart.index` | public (session cart) |
| POST | `/cart/add` | `cart.add` | public |
| POST | `/cart/update` | `cart.update` | public |
| POST | `/cart/remove` | `cart.remove` | public |
| POST | `/cart/clear` | `cart.clear` | public |
| GET | `/checkout` | `orders.checkout_page` | `@login_required` |
| POST | `/checkout` | `orders.checkout_submit` | `@login_required` |
| GET | `/orders` | `orders.history` | `@login_required` |
| GET | `/orders/<int:order_id>` | `orders.detail` | `@login_required` + **ownership** |
| GET/POST | `/preferences` | `preferences.preferences` | `@customer_required` |
| GET | `/recommendations` | `preferences.recommendations` | `@customer_required` |
| GET | `/recommendations/explain` | `preferences.explain` | `@customer_required` |
| GET | `/admin/staff` | `admin_staff.staff_list` | `@require_role(ADMIN)` |
| POST | `/admin/staff/<int:user_id>/approve` | `admin_staff.approve` | `@require_role(ADMIN)` |
| POST | `/admin/staff/<int:user_id>/revoke` | `admin_staff.revoke` | `@require_role(ADMIN)` |
| GET | `/staff/orders` | `staff_orders.order_list` | `@require_role(STAFF, ADMIN)` |
| GET | `/staff/orders/<int:order_id>` | `staff_orders.order_detail` | `@require_role(STAFF, ADMIN)` |
| POST | `/staff/orders/<int:order_id>/status` | `staff_orders.order_status_update` | `@require_role(STAFF, ADMIN)` |
| GET | `/admin/menu` | `admin_menu.menu_list` | `@require_role(STAFF, ADMIN)` |
| GET/POST | `/admin/menu/create` | `admin_menu.menu_create` | `@require_role(ADMIN)` |
| GET/POST | `/admin/menu/<int:item_id>/edit` | `admin_menu.menu_edit` | `@require_role(ADMIN)` |
| GET | `/admin/categories` | `admin_menu.category_list` | `@require_role(ADMIN)` |
| GET/POST | `/admin/categories/create` | `admin_menu.category_create` | `@require_role(ADMIN)` |
| GET/POST | `/admin/categories/<int:category_id>/edit` | `admin_menu.category_edit` | `@require_role(ADMIN)` |

Two details worth noticing:

* **The cart routes are public.** The cart is a session cookie, so an anonymous visitor
  can build one. Authentication is required at `/checkout`, not before.
* **`/admin/menu` (the list) allows STAFF**, but every create/edit route under
  `/admin` requires ADMIN. Staff can see the full menu including unavailable items;
  only an admin can change it.

### 4.3 The generic route pattern

```text
Browser request
      ↓
Flask matches the URL rule → calls the view function
      ↓
Authorization decorator      @login_required / @require_role(...)
   not logged in  → 401 (API) or redirect to /login?next=... (browser)
   wrong role     → 403
      ↓
Form / input validation      WTForms; CSRF token checked here for POST
   invalid → re-render the template with inline field errors
      ↓
Service call                 app/services/*  — plain arguments, no flask.*
   raises ValidationError subclass on a business-rule failure
      ↓
SQLAlchemy → MySQL
      ↓
render_template(...)         Jinja2, autoescaped
      ↓
HTML response
```

A concrete example, `preferences.recommendations`, in full:

```python
@preferences_bp.get("/recommendations")
@login_required
def recommendations():
    user = get_current_user()
    preference = get_preferences(user.id)
    results = recommend_for_user(user.id, preference)
    return render_template(
        "preferences/recommendations.html",
        recommendations=results,
        preference=preference,
    )
```

Five lines. The route authorizes, fetches, delegates, and renders. **There is no
`user_id` parameter** — the identity comes from `get_current_user()`, which reads the
server-side session. That absence is the IDOR defence, and the module docstring says so:
*"a forged one has nothing to bind to, because the only id that reaches the service
layer is the authenticated one."*

---

## 5 — Authentication and authorization

Implemented across `app/routes/auth.py`, `app/services/auth.py`,
`app/utils/authorization.py` and `app/utils/security.py`.

### 5.1 Registration

`GET/POST /register`. `RegistrationForm` collects username, email, password, and
password confirmation.

Server-side policy, from `app/utils/security.py`:

| Rule | Value |
| --- | --- |
| Username pattern | `^[a-zA-Z0-9_]{3,32}$` |
| Password minimum length | **10** characters |
| Password maximum length | 128 (bounds the work Argon2 does on attacker input) |
| Password must contain | at least one letter **and** at least one digit |
| Password deny-list | 10 of the most common breached passwords |

Username and email are lowercased before storage (`normalize_username`,
`normalize_email`) so uniqueness does not depend on the database collation.

**Registration signs the new account in.** The comment in `auth.py` explains the
reasoning and, importantly, the security position:

> *"This is the same `login_user()` the login view calls — it clears any pre-existing
> session first (fixation protection) and stamps the session version, so the account is
> authenticated on exactly the same terms as a normal login. Nothing about
> authorization changes: `register_user()` assigns the role, and it is still
> CUSTOMER."*

The user is then redirected to `/preferences` with a welcome flash.

### 5.2 Login

`GET/POST /login`. `LoginForm` collects `username` and `password`.

1. Rate-limit check — `login_limiter.is_limited(rate_key)`. The limiter is
   **5 attempts per 15 minutes** (`app/utils/ratelimit.py`).
2. `authenticate_user(username, password)` — looks up the normalised username and
   verifies the Argon2id hash.
3. On failure: record the attempt, audit `LOGIN_FAILURE`, flash a message.
4. On success: reset the limiter, `login_user(user)`, audit `LOGIN_SUCCESS`, redirect.

**The failure message is deliberately identical** whether the username exists or not:

```python
# Deliberately generic: does not say which of username/password
# was wrong, and identical whether or not the username exists.
flash("Invalid username or password.", "error")
```

That removes the user-enumeration oracle.

> **Known limitation:** `app/utils/ratelimit.py` is an in-memory dict-and-lock limiter.
> Its own docstring is candid — it *"resets on restart or across gunicorn workers"*.
> Real protection for a single process; a multi-worker deployment needs a shared store.
> `docs/SECURITY.md` §17 lists this.

### 5.3 Logout and session revocation

`POST /logout` — a POST, not a GET, so it cannot be triggered by an image tag or a
link, and it carries a CSRF token.

Flask's session is a **signed cookie with no server-side store**. `session.clear()`
clears only the browser's copy; a cookie captured beforehand kept authenticating. That
was a real finding during this project, and the fix is `User.session_version`:

```python
def logout_user() -> None:
    user = get_current_user()
    if user is not None:
        user.session_version = (user.session_version or 0) + 1
        db.session.commit()
    session.clear()
```

`login_user` copies the current counter into the session; `get_current_user` compares
the two on **every** request and clears the session on mismatch. Incrementing the column
therefore invalidates every session issued before that logout.

Two deliberate consequences:

* **Logging out on one device logs the account out everywhere.** Distinguishing
  sessions would need per-session server-side state; a single counter needs one integer
  column and no new subsystem.
* **Sessions issued before this feature existed carry no version at all and are
  refused.** Failing closed is intended: *"a session that cannot prove it is current
  does not authenticate."*

If the commit fails, the cookie is still cleared — the browser in hand is logged out
regardless.

### 5.4 What the session actually holds

Exactly two values:

| Key | Value |
| --- | --- |
| `user_id` | The primary key of the `users` row |
| `sv` | The user's `session_version` at login time |

**No role. No permissions. No trust decision of any kind.** The role is re-read from
the database on every request, which is what makes a server-side role change or account
deactivation take effect on the user's *very next request* rather than when their
session expires.

The cookie is signed with `SECRET_KEY` via itsdangerous — **tamper-evident, but not
encrypted**. Neither stored value is secret, so that is fine.

### 5.5 The two decorators

```python
@login_required                       # any authenticated user
@require_role(Role.STAFF, Role.ADMIN) # authenticated AND role in the list
```

`require_role` implies `login_required`. An anonymous request gets 401/redirect; an
authenticated request with the wrong role gets 403. That distinction matters to a
client ("log in" vs "you may not"), and neither response reveals anything about
resources the caller cannot reach.

### 5.6 Status codes, and what each means here

| Code | Meaning in Quick Junction | Where it comes from |
| --- | --- | --- |
| **400** | Malformed or rejected request. **A POST without a valid CSRF token lands here.** | Flask-WTF's `CSRFProtect`, and Werkzeug for malformed input |
| **401** | *"You are not authenticated."* Returned to **API clients** only — a browser gets a 302 to the login form instead | `_unauthorized()` in `app/utils/authorization.py` |
| **403** | *"You are authenticated, but your role does not permit this."* | `_forbidden()` |
| **404** | *"There is no such resource — for you."* Used for a real-but-not-yours order, deliberately in preference to 403 | `abort(404)` and ownership-scoped queries |

**Why a foreign order returns 404 and not 403.** A 403 confirms the order exists. A
404 does not distinguish "no such order" from "not yours", so it leaks nothing. The
implementation makes this structural rather than a matter of remembering to check:

```python
def get_order_for_user(order_id: int, user_id: int) -> Order | None:
    """Ownership is enforced in the query itself, not checked after the
    fact -- the IDOR-safe way to answer "does this order belong to this
    user": a mismatched id and a non-existent id both return ``None``."""
    return db.session.query(Order).filter_by(id=order_id, user_id=user_id).first()
```

### 5.7 CSRF protection

`CSRFProtect` is initialised application-wide in `create_app`. Every state-changing
form includes a token, either through WTForms' `{{ form.hidden_tag() }}` or an explicit
hidden input (the logout form in `base.html` uses `{{ csrf_token() }}`).

`WTF_CSRF_TIME_LIMIT = None` means tokens do not expire independently of the session —
so a form left open on a screen does not fail mysteriously. It is disabled only in
`TestingConfig`.

### 5.8 Content negotiation — HTML or JSON

`wants_html()` in `app/utils/errors.py`:

```python
def wants_html() -> bool:
    accept = request.accept_mimetypes
    return accept["text/html"] > accept["application/json"]
```

The strict `>` is load-bearing. The docstring lays out the three cases:

| Client | `Accept` header | Served |
| --- | --- | --- |
| Browser | `text/html` 1.0, `*/*` 0.8 | **HTML** |
| `curl` (no preference) | `*/*` — both score 1.0 | **JSON** |
| API client / test client | absent or JSON | **JSON** |

An equal-quality match falls through to JSON, which is why adding styled HTML error
pages could not change the behaviour any existing API client or test already depended
on.

### 5.9 The `next` redirect and open-redirect protection

When an unauthenticated browser hits a protected page, `_unauthorized()` redirects to
`/login?next=<original path>`. `safe_next_target()` in `auth.py` validates it before
following:

```python
target = request.args.get("next", "")
if not target.startswith("/") or target.startswith("//") or "\\" in target:
    return None
if urlparse(target).path == url_for("auth.login"):
    return None
return target
```

| Rule | Rejects |
| --- | --- |
| must start with `/` | `https://evil.example/` |
| must not start with `//` | `//evil.example` (protocol-relative) |
| must not contain `\` | browsers that normalise `\` to `/` |
| must not be `/login` | a redirect loop |

Without this, the application's own login page becomes a phishing primitive — *"which
is a convincing phishing primitive precisely because the first hop is genuine."*

> ### ⚠ Known defect — `?next=` never actually takes effect from a browser
>
> `login.html` posts to `url_for('auth.login')` **without the query string**:
>
> ```html
> <form method="post" action="{{ url_for('auth.login') }}" novalidate>
> ```
>
> `safe_next_target()` reads `request.args.get("next")`, which is empty on that POST.
> The result is that a browser login **always** lands on `/account/`, never on the
> page the user originally wanted.
>
> The tests in `tests/test_demo_polish.py` pass because they POST directly to
> `/login?next=/orders`, which a browser never does. The validation logic is correct;
> only the form action drops the parameter.
>
> **Fix, if you take it on:** carry `next` through the form action or as a hidden
> field, and add a test that follows the redirect the way a browser does. Low
> priority — it is invisible in normal use.

---

## 6 — Frontend architecture

### 6.1 Server-rendered, deliberately

There is **no JavaScript framework, no bundler, no build step, and no client-side
routing**. Every page is a complete HTML document rendered by Jinja2 on the server and
returned in one response.

The practical consequences:

* You can view-source any page and see the final markup.
* There is nothing to compile. Edit a template, refresh the browser.
* The application works with JavaScript disabled. The only JS is a progressive
  enhancement (§6.8).
* State lives on the server (database) or in the signed session cookie (cart, login).
  Nothing meaningful lives in the browser.

### 6.2 Where the frontend lives

```text
app/templates/
├── base.html                       the shared layout every page extends
├── home.html                       public landing page
├── login.html                      login form
├── register.html                   registration form
├── account.html                    role-aware account dashboard
├── partials/
│   ├── _forms.html                 field / select / checkbox / submit macros
│   ├── _menu_card.html             one menu item card
│   └── _status_badge.html          coloured order-status badge
├── menu/
│   ├── index.html                  menu grid + category filter
│   └── detail.html                 one dish + add-to-cart form
├── cart/
│   └── index.html                  cart table + summary
├── orders/
│   ├── checkout.html               order review + Place order
│   ├── detail.html                 one order
│   └── history.html                My orders
├── preferences/
│   ├── form.html                   the three-dimension preference form
│   ├── recommendations.html        ranked cards with match bars
│   └── explain.html                two-panel AI explanation page
├── staff/
│   ├── order_list.html             the order queue
│   └── order_detail.html           one order + legal status buttons
├── admin/
│   ├── category_list.html          categories
│   ├── category_form.html          create/edit a category
│   ├── menu_list.html              all items, including unavailable
│   └── menu_form.html              create/edit a menu item
└── errors/
    ├── _layout.html                shared error-page shell
    ├── 403.html   404.html   500.html
    └── generic.html                any other status code

app/static/
├── css/app.css                     ~40 lines of project-specific CSS
├── js/loading.js                   ~60 lines, progressive enhancement
└── vendor/
    ├── bootstrap.min.css           Bootstrap 5, vendored
    └── bootstrap.bundle.min.js     Bootstrap 5 JS, vendored
```

### 6.3 `base.html` and template inheritance

Every page starts with:

```jinja
{% extends "base.html" %}
{% block title %}...{% endblock %}
{% block content %} ... {% endblock %}
```

`base.html` provides the `<head>`, the navbar, the flash-message area, the `<main>`
container, the footer, and the two `<script>` tags. A child template fills in `title`
and `content` and nothing else.

**Bootstrap is vendored, not loaded from a CDN.** The comment at the top of
`base.html`:

> *"this project is offline-first (local model, no external services), so a CDN would
> add a runtime dependency on the public internet that nothing else here has."*

**Autoescaping is on and no template uses `|safe`.** That is stated in the same
comment and it matters: *"every value below is rendered as text, including menu names
and AI output."* Model-generated text is untrusted text, and it is never rendered
unescaped.

### 6.4 Role-aware navigation

```jinja
{% if current_user %}
  <li><a href="{{ url_for('preferences.recommendations') }}">Recommended</a></li>
  <li><a href="{{ url_for('orders.history') }}">My orders</a></li>
  {% if current_user.role.value in ("staff", "admin") %}
    <li><a href="{{ url_for('staff_orders.order_list') }}">Order queue</a></li>
  {% endif %}
  {% if current_user.role.value == "admin" %}
    <li><a href="{{ url_for('admin_menu.menu_list') }}">Manage menu</a></li>
  {% endif %}
{% endif %}
```

The comment beside it states the security position exactly:

> *"Display convenience only. Every staff/admin route re-checks the role server-side
> via `require_role` — hiding a link is not the control."*

`current_user` and `cart_count` reach the template through the context processor
registered in `create_app` (§3.2 step 10), so no route has to pass them.

### 6.5 Partials

**`partials/_forms.html`** — four macros wrapping Bootstrap's form markup:

```jinja
{% macro field(f, autofocus=false, help=none, type=none) %}
{% macro select(f, help=none) %}
{% macro checkbox(f, help=none) %}
{% macro submit(label, classes="btn btn-primary") %}
```

Its comment records a real design decision:

> *"Bootstrap validation feedback is driven by the **server's** WTForms errors
> (`is-invalid` + `invalid-feedback`), not by client-side validation, so the feedback a
> user sees is always the same judgement the server made. Forms carry `novalidate` for
> the same reason."*

That is why every form in this project has `novalidate` on it. It is not an oversight.

The `submit` macro disables the button and swaps in a spinner on click, so a slow
request (checkout, AI explanation) gives feedback and cannot be double-submitted by an
impatient click. The comment is careful to add: *"The server is still the authority on
duplicate submissions — this is UX, not a control."*

**`partials/_menu_card.html`** — one dish card. Used by the menu grid, the home page
preview, and the recommendations page, which is why all three look consistent.

**`partials/_status_badge.html`** — a coloured badge per `OrderStatus`.

### 6.6 Flash messages

Set in a route with `flash("Preferences saved.", "success")` and rendered once by
`base.html`:

```jinja
{% for category, message in messages %}
  <div class="alert alert-{{ 'danger' if category == 'error' else 'success' }} ...">
```

Only two categories are used across the project: `success` and `error`.

### 6.7 The `money` filter

`app/utils/formatting.py` registers one Jinja filter, used everywhere a price appears:

```jinja
{{ item.price | money }}   →   ₹249.00
```

Three properties worth knowing:

* The symbol is a module constant, `CURRENCY_SYMBOL = "₹"` — *"Named rather than
  inlined so there is one place to change it."*
* It stays inside `Decimal` throughout. A string or int is converted via `str()`,
  **never** through `float()`, because *"binary floating point cannot represent most
  decimal fractions exactly and this is the one place where being a cent out is visible
  to a customer."*
* `None` renders as an em dash `—`, not `₹0.00`. The comment: *"An em dash reads as
  'not applicable'; a zero would be a lie."* This is what a cart line shows when its
  dish has been withdrawn.

### 6.8 JavaScript

Two files, both loaded by `base.html`:

1. **`vendor/bootstrap.bundle.min.js`** — needed for the navbar collapse on narrow
   screens and dismissible alerts. Not modified.
2. **`js/loading.js`** — ~60 lines, no dependencies. It listens for clicks on any
   element carrying a `data-loading` attribute and swaps in a spinner.

Why it exists is a genuinely instructive piece of reasoning, from its own header:

> *"One page in this application is genuinely slow: `/recommendations/explain` runs the
> local language model on CPU… The page is rendered server-side, so by the time it
> arrives the work is already finished — which means a spinner **inside** that page
> would never be seen. The wait happens during navigation, so that is where the
> feedback has to go."*

It is careful about three things: it does not stack indicators on repeated clicks, it
lets modified clicks (Ctrl-click, middle-click) behave normally, and it announces the
change to assistive technology through the `#global-status` live region in `base.html`.

> **All of it is progressive enhancement.** With JavaScript disabled every link and
> form still works, just without the indicator.

### 6.9 `app.css`

About 40 lines. Its own header sets the policy: *"Deliberately short: anything
Bootstrap already provides is used as-is rather than re-implemented."*

The classes that carry meaning:

| Class | Purpose |
| --- | --- |
| `.qj-price` | `tabular-nums` so price columns align |
| `.qj-facts-panel` | Green left border — **the authoritative panel** |
| `.qj-ai-panel` | Blue left border — **the advisory panel** |
| `.qj-empty` | Empty-state styling |
| `.qj-card-title` | Menu card title size |

The two panel classes are not decoration. The comment says so:

> *"The AI explanation block is visually distinct from the deterministic facts beside
> it, so a reader can tell at a glance which is authoritative."*

### 6.10 Responsive behaviour

Bootstrap's grid, with `<meta name="viewport" content="width=device-width,
initial-scale=1">` in `base.html` and a `navbar-toggler` for narrow screens. Tables use
Bootstrap's responsive wrappers.

> **Honest note:** a readiness audit on 2026-08-20 verified the desktop rendering of
> every page but was **unable to confirm narrow-viewport behaviour** — the browser
> resize did not take effect during the check. The markup uses standard Bootstrap
> responsive classes throughout, so it is very likely fine, but it is untested.
> If mobile matters to you, test it explicitly.

---

## 7 — Frontend → backend communication

Every interaction is a plain HTML form POST or a link GET. **There are no JSON API
endpoints and no `fetch()` calls anywhere in this project.** The JSON responses in
§5.8 exist only for error bodies served to non-browser clients.

### 7.1 Login

```text
Browser: POST /login
   form fields: csrf_token, username, password
        ↓
app/routes/auth.py::login
        ↓
login_limiter.is_limited(rate_key)          5 attempts / 15 min
        ↓
LoginForm().validate_on_submit()            CSRF verified here
        ↓
app/services/auth.py::authenticate_user()
   normalize_username → SELECT users WHERE username = ?
   verify_password()  → Argon2id verify
        ↓
   FAILURE → login_limiter.record_failure()
             record_event(LOGIN_FAILURE)
             flash("Invalid username or password.")
             re-render login.html   (200)
        ↓
   SUCCESS → login_limiter.reset()
             login_user(user)       session.clear(); user_id; sv
             record_event(LOGIN_SUCCESS)
             redirect → /account/   (302)
```

### 7.2 Preferences

```text
Browser: POST /preferences
   form fields: csrf_token, dietary_preference, cuisine_preference, spice_preference
                (each may be the empty string = "no preference")
        ↓
app/routes/preferences.py::preferences
        ↓
PreferenceForm().validate_on_submit()
        ↓
get_current_user()                          identity from the session
        ↓
app/services/preferences.py::update_preferences(user.id, PreferenceInput(...))
   _coerce(): "" → None, otherwise must be a real enum member
   row located BY user_id, never by a client-supplied row id
   INSERT if absent, UPDATE if present
   db.session.commit()
        ↓
flash("Preferences saved.")
redirect → /recommendations   (302)
```

Note `_coerce`. The `<select>` elements only offer valid options, but *"a raw POST can
always skip the `<select>`"* — so every non-empty value is re-validated against its
enum server-side, and an invalid one produces a field error rather than an exception.

### 7.3 Add to cart

**No database write happens here.**

```text
Browser: POST /cart/add
   form fields: csrf_token, menu_item_id, quantity
        ↓
app/routes/cart.py::add
        ↓
AddToCartForm().validate_on_submit()
        ↓
get_cart()                                  read the signed session cookie
        ↓
app/services/cart.py::add_item(cart, menu_item_id, quantity)
   validate_quantity()          1..20 per item
   get_available_menu_item()    SELECT — the ONLY database read here.
                                Same "available item in active category" filter
                                as the public menu.
   cap: 30 distinct items
   returns a NEW dict — the service is pure
        ↓
save_cart(new_cart)                         write back to the session cookie
        ↓
redirect (302)
```

The cart is `{"<menu_item_id>": quantity}` and **nothing else** — no price, no name, no
total. Flask signs the cookie with `SECRET_KEY`, so a client can read it but cannot
alter it without invalidating the signature. Even so, nothing downstream trusts it:
`build_cart_view()` re-resolves every line against the live `menu_items` table on every
read, and drops a corrupted entry rather than crashing.

### 7.4 Checkout

```text
Browser: POST /checkout
   form fields: csrf_token   (that is all — the cart comes from the session)
        ↓
app/routes/orders.py::checkout_submit
        ↓
@login_required                             authentication required HERE, not earlier
CheckoutForm().validate_on_submit()
        ↓
app/services/orders.py::checkout(user.id, get_cart())
   ┌─ ONE TRANSACTION ────────────────────────────────────────┐
   │ _parse_cart()          reject non-integer keys/values     │
   │ reject > 30 distinct lines                                │
   │ for each line:                                            │
   │    quantity in 1..20?                                     │
   │    get_available_menu_item()  ← re-read from the database │
   │ INSERT orders (subtotal 0, total 0, status pending)       │
   │ db.session.flush()            ← assigns order.id          │
   │ for each line:                                            │
   │    line_total = item.price * qty   (Decimal)              │
   │    INSERT order_items with name + unit price SNAPSHOTS    │
   │ UPDATE orders SET subtotal, total                         │
   │ db.session.commit()                                       │
   └───────────────────────────────────────────────────────────┘
        ↓
   FAILURE → db.session.rollback(); raise CheckoutError
             record_event(ORDER_CREATION_FAILED)
             flash each message; redirect → /checkout
        ↓
   SUCCESS → clear_cart()
             record_event(ORDER_CREATED, metadata={"order_id", "total"})
             flash("Order placed.")
             redirect → /orders/<id>   (302)
```

**Nothing about the price comes from the browser.** The POST body contains only a CSRF
token. Every price is re-read from `menu_items` inside the transaction and then frozen
into `order_items`. The checkout page tells the customer this in plain language:
*"Every price is recalculated on the server from the current menu when you place the
order."*

### 7.5 Staff status change

```text
Browser: POST /staff/orders/<id>/status
   form fields: csrf_token, status
        ↓
@require_role(STAFF, ADMIN)                 403 for a customer
        ↓
get_order_for_staff(order_id)               staff may see ANY order — that is the job
   None → abort(404)
        ↓
OrderStatusForm().validate_on_submit()
        ↓
coerce_status(form.status.data)             string → OrderStatus enum
update_order_status(order, new_status)
   is_valid_transition(current, new)?       ALLOWED_STATUS_TRANSITIONS
        ↓
   REJECTED → record_event(ORDER_STATUS_CHANGE_REJECTED,
                           metadata={"from", "attempted"})
              flash; redirect back
        ↓
   ACCEPTED → UPDATE orders SET status
              record_event(ORDER_STATUS_CHANGED, metadata={"from", "to"})
              redirect back
```

**Rejected transitions are audited too.** The comment explains why: *"a repeated
attempt to force an order backwards is exactly the kind of thing worth seeing."*

### 7.6 The AI explanation

See §26 for the full end-to-end trace. In summary: `GET /recommendations/explain` runs
the recommendation engine, takes `results[0]`, builds an `ExplanationRequest`, calls
`generate_explanation()`, and renders both panels. It is a **GET with no parameters** —
everything comes from the session identity and the database.

---

## 8 — The Python backend

### 8.1 The four kinds of module, and how to tell them apart

| Layer | Directory | May import | May touch `flask.request` / `session` | Example |
| --- | --- | --- | --- | --- |
| **Route** | `app/routes/` | services, utils, forms | **Yes** | `app/routes/orders.py::checkout_submit` |
| **Service** | `app/services/` | models, other services | **No** | `app/services/orders.py::checkout` |
| **Model** | `app/models/` | `db`, other models | **No** | `app/models/order.py::Order` |
| **Utility** | `app/utils/` | anything | Yes, where that is its job | `app/utils/authorization.py` |

The service-layer rule is stated explicitly in several docstrings. From
`app/services/preferences.py`:

> *"No `flask.request` or `flask.session` here: callers pass a `user_id` they have
> already authenticated. Nothing in this module accepts a user id from a client."*

And from `app/services/cart.py`:

> *"No function here reads `flask.request` or `flask.session` — callers
> (`app/routes/cart.py`) own the session and pass/receive plain dicts."*

**This is the single most important convention in the codebase.** It is what makes the
services unit-testable without a request context, and it is what makes the IDOR
guarantee auditable: if no service accepts a client-supplied identity, none can be
tricked into acting on the wrong one.

### 8.2 Where business logic belongs

**In `app/services/`.** A route should read like a short script: authorize, validate,
call a service, respond.

When you are unsure, ask: *would this rule still be true if the request arrived over
something other than HTTP?* If yes, it is a service concern.

| Belongs in a route | Belongs in a service |
| --- | --- |
| Reading form fields | Deciding whether a value is acceptable |
| Reading the session | Computing a total |
| Choosing a template | Deciding whether a status transition is legal |
| Setting a flash message | Writing to the database in a transaction |
| Choosing a redirect | Deciding what a customer may be shown |

### 8.3 Error handling in the service layer

One shared exception type, in `app/services/errors.py`:

```python
class ValidationError(Exception):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__("validation failed")
        self.errors = errors
```

The `errors` dict maps a **field name** to a list of messages, which is exactly the
shape a form needs to show them inline. Domain subclasses exist purely for readability
at the call site: `RegistrationError`, `PreferenceError`, `CartError`, `CheckoutError`,
`OrderStatusError`, `CategoryError`, `MenuItemError`.

The route-side pattern is consistent:

```python
except PreferenceError as exc:
    for field_name, messages in exc.errors.items():
        if hasattr(form, field_name):
            getattr(form, field_name).errors.extend(messages)
        else:
            for message in messages:
                flash(message, "error")
```

Known field → inline error next to that field. Unknown field → a flash message.

### 8.4 Forms

All in `app/routes/forms.py`, all subclassing `FlaskForm` so CSRF is automatic. The
important ones:

| Form | Used by |
| --- | --- |
| `RegistrationForm`, `LoginForm` | `auth.py` |
| `PreferenceForm` | `preferences.py` — three `SelectField`s |
| `AddToCartForm`, `UpdateCartForm`, `RemoveFromCartForm`, `ClearCartForm` | `cart.py` |
| `CheckoutForm` | `orders.py` — **only a submit button** |
| `CategoryForm`, `MenuItemForm` | `admin_menu.py` |
| `OrderStatusForm` | `staff_orders.py` |

**WTForms validation is not the only validation.** The service layer re-validates
everything independently. WTForms produces good error messages; the service is the
authority.

### 8.5 Utilities

| Module | Provides |
| --- | --- |
| `app/utils/authorization.py` | `login_user`, `logout_user`, `get_current_user`, `login_required`, `require_role` |
| `app/utils/security.py` | `hash_password`, `verify_password`, `needs_rehash`, `password_policy_errors`, `normalize_email`, `normalize_username` |
| `app/utils/errors.py` | `wants_html`, `render_error`, `register_error_handlers` |
| `app/utils/formatting.py` | the `money` Jinja filter |
| `app/utils/logging.py` | `configure_logging`, `get_security_logger` |
| `app/utils/cart.py` | `get_cart`, `save_cart`, `clear_cart` — session access only |
| `app/utils/ratelimit.py` | `login_limiter` |
| `app/utils/request_meta.py` | `client_ip`, `user_agent` for audit rows |

> **`needs_rehash()` is written but never called.** `docs/SECURITY.md` §17 lists
> "login-time rehash" as outstanding, needed at the first Argon2 parameter change.
> It is a known gap, not an oversight.

---

## 9 — SQLAlchemy and the database layer

### 9.1 Models

Declarative, using SQLAlchemy 2.0's typed `Mapped[...]` / `mapped_column(...)` style:

```python
class Category(db.Model):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    menu_items: Mapped[list["MenuItem"]] = relationship(back_populates="category")
```

`db.Model` comes from Flask-SQLAlchemy. `Mapped[int]` gives real type-checker support.

Full table-by-table detail is in **`docs/MYSQL_SETUP_HANDOFF.md` §10** and
`docs/DATABASE.md`.

### 9.2 Relationships

| Relationship | Kind | Mechanism |
| --- | --- | --- |
| `Category` ↔ `MenuItem` | one-to-many | `back_populates` both ways |
| `MenuItem` ↔ `Ingredient` | many-to-many | `secondary=MenuItemIngredient.__table__` |
| `User` → `CustomerPreference` | one-to-one | `user_id` is `UNIQUE` on the child |
| `Order` ↔ `OrderItem` | one-to-many | `cascade="all, delete-orphan"` |
| `Order` → `User` | many-to-one | read-only `customer` relationship for the staff queue |
| `OrderItem` → `MenuItem` | many-to-one | |
| `AuditLog` → `User` | many-to-one, nullable | `ondelete="SET NULL"` |

The `Order.customer` relationship carries a comment worth noting: it *"deliberately
exposes the account, not a copy of any personal detail — the template renders username
only."*

### 9.3 Foreign-key delete behaviour, and why each was chosen

| Foreign key | `ondelete` | Reason |
| --- | --- | --- |
| `menu_items.category_id` | `RESTRICT` | A category with items cannot be deleted. Categories are *deactivated* instead |
| `orders.user_id` | `RESTRICT` | *"an order is a financial record and must never be silently orphaned"* |
| `order_items.order_id` | `CASCADE` | Deleting an order removes its own lines |
| `order_items.menu_item_id` | `RESTRICT` | A historical order line must never lose its reference |
| `customer_preferences.user_id` | `CASCADE` | Preferences are meaningless without their user |
| `menu_item_ingredients.*` | `CASCADE` | The association is meaningless without both ends |
| `audit_logs.user_id` | `SET NULL` | *"keeps the audit row (and its metadata) after an account is removed"* |

### 9.4 Money handling — the rule

**Every monetary value is `Decimal` in Python and `DECIMAL(10,2)` in MySQL. Never
`float`. Anywhere.**

```python
price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2), nullable=False)
```

Arithmetic stays inside `Decimal` and quantizes explicitly:

```python
line_total = (item.price * line.quantity).quantize(Decimal("0.01"))
```

The display filter converts via `str()`, never `float()` (§6.7). Database `CHECK`
constraints back all of it up: `price > 0`, `subtotal >= 0`, `total >= 0`,
`quantity > 0`, `line_total >= 0`.

### 9.5 Sessions, commits and rollbacks

`db.session` is a scoped session managed by Flask-SQLAlchemy — one per request,
cleaned up automatically at the end.

The transactional pattern, from `checkout`:

```python
try:
    db.session.add(order)
    db.session.flush()            # assign order.id without committing
    for line, item in resolved:
        db.session.add(OrderItem(...))
    order.subtotal = subtotal
    order.total = subtotal
    db.session.commit()
except SQLAlchemyError:
    db.session.rollback()
    raise CheckoutError({"cart": ["Could not place your order. Please try again."]})
```

Three things to copy from it:

* **`flush()` vs `commit()`.** `flush()` sends the INSERT so the database assigns
  `order.id` for the child rows' foreign key, without ending the transaction.
* **Always roll back on `SQLAlchemyError`.** Leaving a session dirty poisons every
  later query in the request.
* **Never leak the driver's exception to the caller.** Catch it, roll back, raise a
  domain error with a message a user can read.

### 9.6 Eager loading, and why it is there

Two queries in `recommendations.py` use eager loading, with a measured justification:

```python
.options(joinedload(MenuItem.category), selectinload(MenuItem.ingredients))
```

> *"`build_item_document()` reads `item.category.name` and `item.ingredients` for every
> candidate. Without eager loading that is one extra query per item (an N+1 measured at
> 14 statements for a 9-item menu in M08); with it, the whole candidate set costs
> three."*

`list_orders_for_staff()` does the same for `Order.items` and `Order.customer`, so the
queue's per-order item count does not fire one query per row.

**If you add a template loop that touches a relationship, check for an N+1.**

### 9.7 Browser action → database operation, worked

```text
Customer clicks "Add to cart" on Paneer Tikka
        ↓
POST /cart/add   {csrf_token, menu_item_id: 1, quantity: 1}
        ↓
app/routes/cart.py::add
   AddToCartForm().validate_on_submit()      CSRF verified
   cart = get_cart()                          {} from the session cookie
        ↓
app/services/cart.py::add_item({}, 1, 1)
   validate_quantity(1)                       → []
   get_available_menu_item(1)
        ↓
   SQLAlchemy:
       SELECT menu_items.* FROM menu_items
       JOIN categories ON categories.id = menu_items.category_id
       WHERE menu_items.id = %s
         AND menu_items.is_available IS true
         AND categories.is_active IS true
       LIMIT 1
        ↓
   MySQL returns one row → MenuItem(id=1, name='Paneer Tikka', price=Decimal('249.00'))
        ↓
   returns {"1": 1}
        ↓
save_cart({"1": 1})                           session["cart"] = {"1": 1}
        ↓
302 → /menu/1
        ↓
Browser re-requests the page; base.html's context processor
sums the cart and renders the navbar badge as "1"
```

**Note the asymmetry.** A `SELECT` happened; no `INSERT`, `UPDATE` or `DELETE` did.
Adding to the cart writes nothing to the database. The first write in the whole
customer journey is the `INSERT` at checkout.

---

## 10 — The recommendation engine

`app/services/recommendations.py` — 240 lines. **This is the component that decides
what a customer is shown.**

### 10.1 The headline

The module docstring opens with it:

> **"This engine is deterministic and does not use the Local Qwen model yet.**
> No LLM, no external API, no network access, no embeddings service, no vector
> database: TF-IDF over the menu's own text plus cosine similarity, computed in-process
> with scikit-learn. **The same inputs always produce the same ranking.**"

### 10.2 The five steps

```text
1. CANDIDATE SET
   SELECT available items in active categories, ordered by name.
   Same filter as the public menu — an item a customer could not order
   is never a candidate.
        ↓
2. DIETARY HARD FILTER            ← applied BEFORE any scoring
   _DIETARY_COMPATIBILITY[preference] → allowed set
   Anything outside it is removed from the list entirely.
        ↓
3. FEATURE TEXT
   Each surviving item → one lowercased document:
      name + description + category name + cuisine
      + "spice_<level>" + "diet_<type>" + ingredient names
   NO price. NO database id.
        ↓
4. QUERY TEXT
   Preferences → a document in the same vocabulary:
      "<cuisine> spice_<level> diet_<type>"   (only the parts that are set)
   Optionally, completed orders → a second document at 0.5 weight.
        ↓
5. RANKING
   TfidfVectorizer(token_pattern=r"[a-z0-9_]+").fit_transform(documents)
   cosine_similarity(query_vector, item_matrix)
   sort by (-score, item.name)                ← name breaks ties, so stable
   return the top 10
```

### 10.3 The dietary hard filter

The most important table in the file:

```python
_DIETARY_COMPATIBILITY: dict[DietaryType, frozenset[DietaryType]] = {
    DietaryType.VEGAN:      frozenset({VEGAN}),
    DietaryType.VEGETARIAN: frozenset({VEGAN, VEGETARIAN}),
    DietaryType.EGGETARIAN: frozenset({VEGAN, VEGETARIAN, EGGETARIAN}),
    DietaryType.NON_VEGETARIAN: frozenset(DietaryType),   # no restriction
}
```

Read as: *"a customer holding this preference may be shown items of these types."*

Three things to understand:

* **It is a safety rule, not a ranking hint.** It is applied at step 2, before any
  document is built. Nothing downstream — not similarity, not order history — can
  reintroduce an excluded item.
* **`vegetarian` excludes `eggetarian` deliberately.** The comment: *"in the cuisine
  this menu targets, 'vegetarian' conventionally excludes egg, and the stricter reading
  is the safe one to be wrong about."*
* **`None` means "no restriction stated", not "allow nothing".**
  `_allowed_dietary_types()` returns `None` in that case and the filter is skipped.

### 10.4 Why prices and ids are excluded from the documents

From `build_item_document`:

> *"No price (magnitude is meaningless to TF-IDF) and no database id (carries no taste
> signal)."*

TF-IDF treats everything as a token. The token `"249.00"` carries no ordering
information — it is not "less than" `"399.00"` to a bag-of-words model. Including
prices would add noise, not signal.

### 10.5 Order history

```python
HISTORY_WEIGHT = 0.5
```

> *"Deliberately below 1.0: what someone **says** they want outranks what they happened
> to order before."*

Only `COMPLETED` orders count — *"a pending or cancelled order is not evidence that
anyone enjoyed anything."*

History is also dietary-filtered a second time, so *"a customer who has since gone
vegetarian is not nudged back toward meat by their own history."* The candidate filter
already guarantees restricted items cannot be *returned*; this stops them influencing
the ranking at all.

### 10.6 Low-signal and empty behaviour

| Situation | Result |
| --- | --- |
| No preferences **and** no history | Every candidate returned in name order, each with `score=0.0` |
| No preferences but some history | Ranked by history alone, at 0.5 weight |
| No candidates after the dietary filter | Empty list — an ordinary outcome, not an error |
| Empty menu | Empty list |

With no preferences, no match score exists (`match_score=None`) and every card renders
as **"Suggested"** with "No preferences to compare" — never a match claim the engine
cannot support.

### 10.7 Two numbers: relevance and preference match (Phase 3)

Each `Recommendation` carries two different scores:

| Field | What it is | Shown to the customer? |
| --- | --- | --- |
| `score` | TF-IDF cosine between the query (preferences + 0.5 × history) and the whole item document | **No** — internal tie-breaker only |
| `match_score` | Mean of the per-preference components below, over the preferences the customer actually set | **Yes**, as "Preference match N%" |

**Why the cosine is no longer shown.** It is computed over name, description,
category, ingredients and attribute tokens, so it is diluted by description length and
weighted by how rare a token is on the current menu. Measured on the demo menu before
Phase 3: an item matching all three stated preferences scored 26%, one matching a single
preference scored 62%, and any positive cosine — as low as 2.3% — was labelled
"Fair match". The percentage did not mean what it said.

**Preference-match components** (`app/services/recommendations.py`):

| Preference | 1.0 | 0.5 | 0.0 |
| --- | --- | --- | --- |
| Cuisine (`cuisine_match`) | same cuisine | — | different cuisine |
| Spice (`spice_match`) | same level | one step apart on none→mild→medium→hot→extra hot | two or more steps apart |
| Diet (`dietary_match`) | exact dietary type | compatible but different (e.g. vegan dish for a vegetarian) | — (incompatible items are filtered out before scoring) |

A preference the customer did not set is `None` and is left out of the average.

**Cuisine similarity.** Cuisine is a controlled vocabulary (`Cuisine` enum), so exact
equality *is* the similarity. A TF-IDF "cuisine profile" (all dish text of the preferred
cuisine vs. each item's text) was prototyped and rejected: on the demo menu it rated
Chicken Fajitas (Mexican) 31% similar to Indian and Steamed Rice 19% similar to Italian,
purely through shared words like "chicken", "onion" and "rice".

### 10.8 Match labels

Classified on the **same rounded whole-number percentage the page prints** (half-up),
so the badge and the number can never disagree:

```text
percent >= 80  →  "Strong match"
percent >= 60  →  "Good match"
percent >= 40  →  "Fair match"
otherwise      →  "Suggested"   (shown on the page as "Low match" when preferences are set)
no preferences →  "Suggested"
```

Rationale, from the values the score can take with three preferences set: 3/3 = 100,
2 exact + near spice = 83, 2 exact + 1 miss = 67, 1 exact + 1 near + 1 miss = 50,
1 exact + 2 misses = 33. "Fair" therefore means *at least half of what you asked for*.

The four label strings are unchanged because they are the vocabulary the frozen V4
adapter was trained on; `match_label` (sent to the model) always returns one of them,
and only the page-side `display_label` says "Low match".

The percentage describes how closely a dish fits the stated preferences. It is **not** a
probability that the customer will like it, and the page says so.

**Ranking** is `(match_score desc, cosine desc, name, id)`: a dish never outranks one that
meets more of what the customer asked for, and history/text relevance orders dishes
within the same match level.

### 10.9 Determinism

Three separate mechanisms guarantee it:

1. No randomness anywhere — no sampling, no seed.
2. Ties break on `item.name` then `item.id` ascending, so equal-scoring items never
   shuffle between requests.
3. The vectorizer is fitted fresh on each call from the current menu, so the result is
   a pure function of (menu state, preferences, completed orders).

### 10.10 What it is not

> **The recommendation engine is not the AI.**

They are different files, different technologies, and different jobs:

* The **engine** decides *what* is shown. It is deterministic and it is authoritative.
* The **model** writes a sentence *about* that decision. It is advisory.

The engine returns real `MenuItem` rows. Callers read the price off the model row —
*"the engine never produces a price, and nothing here lets a customer influence one."*

**The application works completely without the AI. It does not work without the
engine.**

---

## 11 — Local AI architecture

### 11.1 The base model

| | |
| --- | --- |
| Model | **Qwen3-0.6B-Base** |
| Path | `models/Qwen3-0.6B-Base` (configurable via `LLM_MODEL_PATH`) |
| Parameters | 596 M (601 M once LoRA layers are attached) |
| Architecture | `Qwen3ForCausalLM`, `model_type: qwen3` |
| Precision | float32, on CPU |
| Weights | `model.safetensors`, ~1.19 GB |

**`Base`, not `Instruct`.** The base variant has no chat-template behaviour of its own,
which is part of why the LoRA style tune was worth doing: the fine-tune establishes the
output style rather than fighting an existing chat persona.

**`transformers >= 4.51` is a hard requirement**, not a preference — that is the
release that added the `qwen3` architecture the model's `config.json` declares.

### 11.2 No cloud API. At all.

> **The application does not call OpenAI, Anthropic, Google, Ollama, or any other
> remote service for the production explanation feature. There is no API key in this
> project because there is no external service to authenticate to.**

This is enforced, not merely intended:

* `app/services/local_llm.py` sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`
  **before** `transformers` is imported anywhere in the process.
* Both the model and the adapter load with `local_files_only=True`.
* A test in `tests/test_llm.py` greps the module source for `openai`, `anthropic`,
  `gemini`, `ollama`, `api_key`, `bearer`, `http://`, `requests.post` and
  `urllib.request`.
* Weights are **safetensors, never Python `pickle`**.

### 11.3 The production combination

```text
   Qwen3-0.6B-Base                    the general language model
        +
   Quick Junction V4 LoRA adapter     20 MB of task-specific weights
        =
   Quick Junction production AI
```

The adapter is applied on top of the frozen base weights at load time by PEFT. Neither
file is modified.

### 11.4 What LoRA means in this project

**LoRA** — Low-Rank Adaptation — trains small additional matrices alongside a frozen
model instead of retraining the model itself.

| | Value |
| --- | --- |
| Rank | 8 |
| Alpha | 16 |
| Dropout | 0.05 |
| Target modules | `q_proj`, `k_proj`, `v_proj`, `o_proj`, `gate_proj`, `up_proj`, `down_proj` |
| Trainable parameters | **5,046,272** |
| Total parameters | 601,096,192 |
| **Trainable percentage** | **0.84 %** |

Those figures are read from `models/qwen3-0.6b-quickjunction-lora-v4/adapter_config.json`
and `training_metrics.json`, not estimated.

**Why it mattered here:** full fine-tuning of 596 M parameters on a CPU is not
practical, and the project forbids requiring a GPU. LoRA is what made fine-tuning
feasible at all — the whole run took about 62 minutes on CPU.

### 11.5 The model's authority — none

`app/services/local_llm.py` opens with this, and it is the governing principle:

> **"The LLM is not the source of truth."** *The deterministic recommendation engine
> decides **what** a customer may be shown; this module only turns that
> already-validated decision into a sentence. It cannot select items, cannot read the
> database, cannot write anything, and is given no route through which a price could
> originate.*

| The model cannot | Because |
| --- | --- |
| Choose a menu item | It is handed one that has already been chosen |
| State a price | `ExplanationRequest` has no price field |
| Name an unavailable item | Such items never survive the engine's candidate filter |
| Judge dietary safety | That was decided by the hard filter before it ran |
| Write to the database | It has no database access of any kind |
| Reach the network | Offline flags, `local_files_only`, no HTTP client |

### 11.6 Prompt injection

Menu names and descriptions are staff-authored, so they are a genuine — if
low-privilege — injection surface. `_sanitise()` bounds and flattens every piece of
free text before it enters the prompt:

```python
_MAX_FREE_TEXT = 120
_UNSAFE_PROMPT_CHARS = re.compile(r"[\r\n\t:#`*<>{}\[\]|]+")
```

**The colon is in that list deliberately.** The prompt is built from `label: value`
lines, so a colon surviving inside free text would let a hostile menu name forge a
field — for example an item called `X Customer preference: cuisine: FAKE`. The comment
records that stripping newlines alone was *not* enough and that a test
(`tests/test_llm.py::test_12`) caught it.

The comment is also honest about its status: *"It is defence in depth, not the primary
control — the primary control is that the model has no authority to act on anything it
reads."*

---

## 12 — Model files

### 12.1 Layout

```text
models/                                       ~1.4 GB total — NOT IN GIT
├── Qwen3-0.6B-Base/                          REQUIRED
│   ├── model.safetensors        1.19 GB      the weights
│   ├── config.json                           declares model_type: qwen3
│   ├── generation_config.json
│   ├── tokenizer.json           7.0 MB
│   ├── tokenizer_config.json
│   ├── vocab.json  merges.txt                BPE vocabulary
│   ├── LICENSE  README.md  .gitattributes
│   └── .cache/
│
├── qwen3-0.6b-quickjunction-lora-v4/         REQUIRED — production adapter
│   ├── adapter_model.safetensors  20.2 MB    the LoRA weights
│   ├── adapter_config.json                   rank, alpha, target modules
│   ├── training_metrics.json                 loss, runtime, parameter counts
│   ├── tokenizer.json  vocab.json  merges.txt
│   ├── tokenizer_config.json  special_tokens_map.json  added_tokens.json
│   ├── chat_template.jinja
│   ├── README.md
│   └── checkpoints/
│
├── qwen3-0.6b-quickjunction-lora/            HISTORICAL (v1) — not needed
├── qwen3-0.6b-quickjunction-lora-v2/         HISTORICAL — not needed
└── qwen3-0.6b-quickjunction-lora-v3/         HISTORICAL — not needed
```

### 12.2 What each part is

| File | Role |
| --- | --- |
| `model.safetensors` | The base model's 596 M weights. Read-only at runtime |
| `adapter_model.safetensors` | The LoRA matrices. Applied on top; the base file is never modified |
| `adapter_config.json` | Tells PEFT the rank, alpha, and which modules to adapt. Also records `base_model_name_or_path` |
| `tokenizer.json` + `vocab.json` + `merges.txt` | Text ↔ token conversion. **The application loads the tokenizer from the *base model* directory**, not the adapter's |
| `training_metrics.json` | The provenance record: dataset version, learning rate, epochs, steps, runtime, loss, parameter counts, device |

### 12.3 Why they are not in Git

`.gitignore` line 64 excludes `/models/`. Three reasons:

1. **Size.** 1.4 GB in a Git repository makes every clone painful and every history
   operation slow.
2. **They are binary artifacts, not source.** The source is
   `data/raw/seed_examples_v4.jsonl` plus `training/train_lora.py`. The adapter is the
   *output*.
3. **`safetensors` are already the safe format**, but committing large opaque binaries
   into a code repository is a habit worth not forming.

`HANDOFF.md` is explicit: *"This repository documents no download source and none
should be assumed."*

### 12.4 How they are transferred

**By the project owner, out of band** — USB drive, network share, or file transfer.
Copy the whole `models/` directory into the repository root.

Verify an installation without starting the web app:

```powershell
python scripts/smoke_local_model.py
```

It loads the base model and generates once. It reads `LLM_MODEL_PATH` if set, otherwise
`models/Qwen3-0.6B-Base` relative to the repository root.

### 12.5 ⚠ Do not modify or regenerate them

> **The model files are frozen.** Do not retrain, do not re-quantise, do not edit
> `adapter_config.json`, do not delete `training_metrics.json`.
>
> `training_metrics.json` is the only record on disk of how the production adapter was
> produced. Losing it does not break the application, but it destroys the provenance
> of the project's central technical claim.
>
> **Back `models/` up** (see `docs/MYSQL_SETUP_HANDOFF.md` §16.4).

---

## 13 — How Python loads the AI

`app/services/local_llm.py` — 674 lines. This section covers loading; §16 covers the
safeguard.

### 13.1 The sequence

```text
Process starts
        ↓
import app.services.local_llm
        ↓
   os.environ.setdefault("HF_HUB_OFFLINE",       "1")
   os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
   os.environ.setdefault("USE_TF",   "0")
   os.environ.setdefault("USE_FLAX", "0")
   os.environ.setdefault("USE_JAX",  "0")
        ↑ AT MODULE LEVEL, before transformers is imported anywhere
        ↓
create_app() → warm_up(app)      (§14) — or lazily, on the first explanation
        ↓
_load()
        ↓
   already failed once?  → return (None, None)          _load_failed flag
   already loaded?       → return the cached pair       module singleton
        ↓
   with _lock:                                          double-checked locking
        ↓
   model_path = _resolve_path("LLM_MODEL_PATH", DEFAULT_MODEL_PATH)
        ↓
   directory missing? → log "Local LLM disabled: model directory not found at ..."
                        set _load_failed = True
                        return (None, None)
        ↓
   import torch, transformers                           deferred until now
        ↓
   AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        ↓
   AutoModelForCausalLM.from_pretrained(model_path,
                                        dtype=torch.float32,
                                        local_files_only=True)
        ↓
   adapter_path = _resolve_path("LLM_ADAPTER_PATH", None)
        ↓
   adapter_config.json present?
        ├─ YES → from peft import PeftModel
        │        PeftModel.from_pretrained(model, adapter_path, local_files_only=True)
        │        log "Loaded LoRA adapter from <path>"
        │        └─ on ANY exception:
        │              log "Could not load LoRA adapter; continuing with base model"
        │              keep the base model
        └─ NO  → continue with the base model, silently
        ↓
   model.eval()
        ↓
   cache (_tokenizer, _model); return them
        ↓
Model ready
        ↓
generate_explanation(request)
```

### 13.2 Configuration keys

| Key | Default | Notes |
| --- | --- | --- |
| `LLM_ENABLED` | `True` (`False` in testing) | Master switch. `False` makes `generate_explanation` return `None` immediately, without loading anything |
| `LLM_MODEL_PATH` | `<repo>/models/Qwen3-0.6B-Base` | Absolute, derived from the repository location |
| `LLM_ADAPTER_PATH` | `<repo>/models/qwen3-0.6b-quickjunction-lora-v4` | **v4 is production** |
| `LLM_MAX_NEW_TOKENS` | `48` | Bounds worst-case latency |
| `LLM_WARMUP` | on in development, **off** in production | §14 |

**Paths are configuration only.** `_resolve_path()` reads `current_app.config` and
nothing else. Its docstring is emphatic:

> *"There is no argument, form field, query parameter or header anywhere in this module
> through which a caller — let alone a browser — can name a path to load."*

### 13.3 The `USE_TF=0` quirk, and why it is not cosmetic

`docs/AI.md` §2 records it:

> *"This machine has a broken TensorFlow installation (its `protobuf` predates
> `runtime_version`), and `transformers` imports TensorFlow opportunistically from
> `image_transforms` — an entirely unrelated code path. Without these flags, importing
> `transformers` **or** `peft` raises `ImportError` before any Quick Junction code
> runs."*

If you write your own script that imports `transformers`, set those variables first
too.

### 13.4 The singleton

```python
_model = None
_tokenizer = None
_load_failed = False
_lock = threading.Lock()
```

Module-level, so **one model per process**. The lock plus the double-check means a real
request arriving mid-warm-up simply waits and reuses the same instance — there is no
path to two concurrent loads within a process.

`_load_failed` is a latch: once loading has failed, later calls return immediately
instead of retrying a 1.2 GB load on every request.

> **Consequence for deployment:** N gunicorn workers means N resident copies. See §14.

### 13.5 Generation settings

```python
outputs = model.generate(
    **inputs,
    max_new_tokens=limit,          # 48 by default
    do_sample=False,               # deterministic: same facts -> same sentence
    pad_token_id=tokenizer.eos_token_id,
)
```

**`do_sample=False` means greedy decoding.** Given the same facts, the same model and
the same library versions, the output is byte-identical every time. That is what makes
the behaviour in §16 reproducible and testable.

### 13.6 `_tidy()` — cosmetic only

Trims the raw continuation to the first two sentences and caps it at 400 characters.

> *"A 0.6B base model rambles and will happily start a new, unrelated paragraph;
> keeping the first couple of sentences is what makes the output presentable. **This is
> cosmetic only — it is not a safety control**, because the model has no authority
> regardless of what it emits."*

### 13.7 Failure handling — the rule

**Every entry point returns `None` rather than raising.** From the module docstring:

> *"Failure is expected and non-fatal. Every entry point returns `None` rather than
> raising, so a missing model, a missing dependency, or a slow CPU degrades the page to
> its deterministic content instead of breaking it."*

The exception handler around loading covers *"Missing dependency, corrupt weights, out
of memory — all the same to a caller: no explanation is available this request."*

And an unusable adapter degrades one step further rather than failing outright:
*"An unusable adapter must not cost us the base model."*

> ### ⚠ Watch for the silent adapter fallback
>
> That last behaviour is correct, but it is quiet. A readiness audit on 2026-08-20
> found the development machine running the **base model** because `peft` was missing
> from the virtual environment. The application worked; the fine-tuned behaviour was
> simply absent.
>
> **Always confirm the log line on startup:**
>
> ```text
> INFO [app.services.local_llm] Loaded LoRA adapter from ...\qwen3-0.6b-quickjunction-lora-v4
> ```
>
> The cause is environment drift, not a bad pin. `requirements-ai.txt` installed
> exactly as written was re-verified on 2026-08-20 in a clean virtual environment:
> no dependency conflicts, V4 loads through this same `_load()`, 420/420 tests pass.
> The fix is to restore the pinned stack —
> `pip install -r requirements.txt -r requirements-ai.txt` — not to bump `peft`.
>
> See `docs/MYSQL_SETUP_HANDOFF.md` §8.4 for the full diagnosis.

---

## 14 — AI warm-up

### 14.1 The problem

Loading 1.2 GB of weights plus the adapter takes time. Because the load is lazy, that
cost lands on whoever asks for the **first** explanation — and in a demonstration, that
first request is the one someone is watching.

### 14.2 What `warm_up()` does

```python
def warm_up(app) -> bool:
    if _warmup_started:              return False   # idempotent
    if app.config.get("TESTING"):    return False   # never in tests
    if not app.config.get("LLM_ENABLED", True):  return False
    if not app.config.get("LLM_WARMUP", False):  return False
    if app.config.get("DEBUG") and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False                                # skip the reloader's parent
    threading.Thread(target=_run, name="llm-warmup", daemon=True).start()
    return True
```

Five guards, then a daemon thread. It is called at the very end of `create_app()` and
returns immediately.

The `WERKZEUG_RUN_MAIN` guard exists because Flask's reloader runs the app in a child
process — *"the parent would warm a model it never serves from."*

### 14.3 Why it cannot break anything

The design constraints are listed in the source:

* It runs on a **daemon** thread, so startup never blocks and shutdown is never held
  open by it.
* It calls the ordinary `_load()`, which is already double-checked locking around a
  module singleton — so a request arriving mid-warm-up waits on the same lock and gets
  the same instance.
* `_load()` never raises. On failure the existing degradation applies unchanged: a
  warning, and explanations return `None`.

### 14.4 Why it is off by default in production

```python
LLM_WARMUP: bool = _env_bool("LLM_WARMUP", False)   # BaseConfig / production
LLM_WARMUP: bool = _env_bool("LLM_WARMUP", True)    # DevelopmentConfig
```

> *"each worker process warms its own copy: N gunicorn workers means N resident 1.2 GB
> models. That is an operator's decision, not a safe default, so production must opt in
> explicitly after checking the memory budget."*

**Four workers with warm-up on is roughly 4.8 GB of resident model.** Turn it on in
production only after doing that arithmetic.

### 14.5 Measured behaviour

> **These are observed measurements on specific hardware on specific dates, not
> guaranteed performance figures.** They will differ on your machine.

**Recorded in the repository** (`config.py` comments and `docs/AI.md` §6, measured
during Milestone 07/07.8 on the development machine):

| | Value |
| --- | --- |
| First explanation, cold (load + generate) | ~18.3 s |
| Subsequent explanation, warm | ~3.7 s |
| `docs/AI.md` §6, fine-tuned, first call | 12.6 s |
| `docs/AI.md` §6, fine-tuned, subsequent call | 6.8 s |

**Measured on 2026-08-20 during the demo-readiness audit**, on the same machine, at
commit `912150a`, with the V4 adapter loaded and MySQL live:

| Operation | Measured |
| --- | --- |
| Process start → serving requests | ~2 s |
| Background warm-up completes | **15.6 s** (does not block anything) |
| Cold model load, cold OS file cache | ~22 s |
| **`GET /recommendations/explain`, warm, over HTTP** | **6.1–6.3 s** |
| `GET /recommendations` (no model involved) | 16–62 ms |
| `GET /menu` | 16–31 ms |
| Login POST (Argon2id) | 125–171 ms |

The two sets disagree — 3.7 s versus 6.1 s for a warm explanation. Both are real
measurements taken at different times with different library versions and different
system load. **Treat 6 seconds as the figure to plan around**, since it is the most
recent and was measured end-to-end over HTTP rather than at the function level.

> **Explanations are not cached.** Every visit to `/recommendations/explain`
> regenerates. Since generation is greedy and deterministic, the result is cacheable in
> principle — it simply is not cached today.

### 14.6 Why the explanation lives on its own route

Because generation is measured in seconds, not milliseconds:

> *"the explanation lives on their own page (`/recommendations/explain`) and
> `/recommendations` never invokes the model."*

The primary recommendation path therefore cannot be slowed or broken by inference. That
is a deliberate isolation boundary, not an accident of routing.

---

## 15 — ExplanationRequest

### 15.1 The definition

```python
@dataclass(frozen=True)
class ExplanationRequest:
    item_name: str
    item_cuisine: str
    item_dietary: str
    item_spice: str
    preferred_cuisine: str | None
    preferred_dietary: str | None
    preferred_spice: str | None
    match_label: str
```

Eight fields. `frozen=True` means it cannot be mutated after construction.

### 15.2 What is deliberately absent

The docstring names the omissions and the reason for each:

> *"Deliberately contains no price, no availability flag and no id: the model has no
> business restating a price, and an unavailable item never reaches this object because
> it never survives the engine's candidate filter."*

| Absent | Consequence |
| --- | --- |
| **Price** | The model cannot restate a price, correctly or otherwise |
| **Database id** | Nothing to leak, nothing to enumerate |
| **Availability flag** | Unreachable — filtered out upstream |
| **Username, email, user id** | No customer PII ever reaches the prompt |
| **Order history** | The model cannot claim "you ordered this before" |
| **Ingredients** | It cannot invent an allergen claim |

### 15.3 Why not just give the model the database

Because the safety properties above would evaporate. A model with database access could
state a price (possibly wrong), name an unavailable dish, reference another customer, or
make a dietary claim the hard filter never sanctioned.

**Constraining the input is what makes the output bounded.** The model can only be
wrong about the eight things it was told. That is a small, enumerable failure surface —
and §16 closes most of it.

### 15.4 Set versus unset — the load-bearing distinction

The three `preferred_*` fields are `str | None`, and **`None` means the customer stated
no preference on that dimension**.

The route builds them from the database:

```python
preferred_cuisine=preference.cuisine_preference.value
                  if preference and preference.cuisine_preference else None,
```

Three ways to get `None`: the customer has no preference row at all; the row exists but
that column is `NULL`; the customer explicitly cleared it.

**This is where `ExplanationRequest` becomes the application's source of truth.** The
model tends to invent preferences the customer never expressed. The application already
*knows* the answer — it is right there in the dataclass — so §16 does not have to
learn it. It just has to check.

### 15.5 The prompt

`build_prompt()` renders the instruction/input format the training dataset was authored
in, so the fine-tuned adapter sees a familiar shape:

```text
Explain in one or two sentences why this menu item was recommended. Use only the facts provided.

Customer preference:
cuisine: indian
dietary: vegetarian
spice: not set

Recommended item:
name: Paneer Tikka
cuisine: indian
dietary: vegetarian
spice: hot
match: Good match

Explanation:
```

**Note `spice: not set`.** That literal placeholder is what appears when the preference
is `None` — and it is exactly what the model sometimes leaks back into its output as
though it were a property of the dish. §16 catches that.

Every value passes through `_sanitise()` first (§11.6).

---

## 16 — The preference safeguard

**This is the most important technical contribution in the project.** Read it carefully
before changing anything in `app/services/local_llm.py`.

### 16.1 The problem, in the model's own words

A language model produces plausible text. Plausible is not the same as supported. Three
real generations, recorded in `docs/AI.md` §18.1:

```text
"...it is Mexican rather than the cuisine you chose."        (none chosen)
"It carries the medium heat you set."                        (none set)
"It is Continental and vegetarian, matching those you set."  (none set)
```

In every case the customer had set **no** preference on that dimension. This is not a
style problem. **The sentence tells the customer something false about their own
account.**

### 16.2 Why a guard rather than a fifth dataset

Milestone 07.6 left V4 at 23/25 with this one failure class outstanding. Three dataset
iterations had tried to teach it away and none removed it.

But the application already holds the authoritative answer —
`ExplanationRequest.preferred_*` is `None` exactly when a preference is unset.

> *"So M07.7 stopped teaching and started checking."*

### 16.3 The flow

```text
generate_explanation(request)
        ↓
model.generate(...)          greedy, max 48 new tokens
        ↓
_tidy(text)                  first 1–2 sentences, ≤400 chars   (cosmetic)
        ↓
apply_preference_safeguard(text, request)      ← UNCONDITIONAL
        ↓
explanation_is_preference_safe(text, request)
   ├─ _leaks_placeholder(text)?                    "not set" as an attribute
   ├─ all three preferences None?
   │     → _claims_any_preference(text)?           any credit at all
   └─ for each dimension with preference None:
         → _claims_unset_preference(text, dim, item_value)?
        ↓
   ┌────────────────┴────────────────┐
  UNSAFE                           SAFE
   ↓                                 ↓
log "Explanation rejected: it       return the model's text
credited a preference the
customer did not set"
   ↓
deterministic_explanation(request)
   built ONLY from request fields
   ↓
return the replacement
```

The safeguard runs **unconditionally**, not as a per-model workaround. The comment:

> *"no adapter has ever been reliably free of unset-preference claims, and the check
> costs a few regex matches against facts the caller already has."*

### 16.4 The three design constraints

From `docs/AI.md` §18.2, and visible throughout the code:

1. **Reject and replace — never edit.**
   *"Deleting a clause from generated prose produces broken English and can silently
   invert meaning."* The sentence is discarded whole.

2. **Detection is dimension-scoped.**
   A pattern fires only when it names the dimension, or that dimension's value, whose
   preference is unset. *"A generic 'matching what you asked for' is not attributed to
   any single dimension and is left alone — the dimensions that ARE set make it true."*

3. **The fallback can invent nothing.**
   It is built only from fields already in `ExplanationRequest`, *"so it can introduce
   no price, ingredient, availability or history — it has none."*

### 16.5 Dimension-scoped detection

`_claims_unset_preference(text, dimension, item_value)` looks for two families of
phrasing.

**By dimension noun** — `cuisine`; `dietary`/`diet`; `spice level`/`spice`/`heat`:

```python
rf"your (?:\w+ )?{n} (?:preference|requirement|level|choice)"
rf"your (?:chosen|stated|selected|preferred|recorded|requested) {n}"
rf"the {n}(?: \w+)? you {_CLAIM_VERB}"
rf"{_MATCH_VERB} your (?:\w+ )?{n}"
rf"{n}(?: \w+)? you {_CLAIM_VERB}"
rf"your preferred {n}"
```

**By the item's value standing in for the dimension** — "you asked for hot", "hot, as
you prefer", "your vegetarian requirement".

The claim verbs cover the ways the model actually writes: `chose`, `chosen`, `choose`,
`set`, `selected`, `select`, `picked`, `asked`, `asked for`, `requested`, `request`,
`wanted`, `want`, `prefer`, `preferred`, `specified`, `specify`, `stated`, `gave`.

**One careful exclusion.** `_AMBIGUOUS_VALUE_WORDS` holds enum values that are also
ordinary English — `other`, `none`, `no heat`, `no spice`, `unspiced` — where a
possessive is not evidence of a claim. "Your other preferences" is innocent phrasing.

### 16.6 The placeholder leak, and how it was found

Two very different sentences contain the same words:

```text
"its heat is hot rather than unset"    ← leakage, and meaningless
"you left the spice level unset"       ← true, and fine to say
```

What separates them is *whose property is being described*, so `_leaks_placeholder()`
judges **per sentence**: a sentence that states something about *you* is left alone;
otherwise the placeholder must not appear in attribute position.

`docs/AI.md` §18.4 records how this was discovered — and it is a good lesson:

> *"Twelve scenarios were driven through the real app on MySQL 8.0.46 — real login,
> real `POST /preferences`, real `GET /recommendations/explain`. The first pass
> surfaced a defect the 25-case harness never produced:*
>
> *"Paneer Tikka is Indian and vegetarian as you prefer; its heat is hot rather than
> **unset**."*
>
> *The model paraphrases the placeholder as the single word `unset`, which the guard's
> two-word `not set` pattern missed."*

A first fix using a fixed-width lookback also failed, because *"as you prefer"* sat
just inside the window. The working version is the per-sentence judgement described
above.

### 16.7 The all-unset case

When every preference is `None`, `_claims_any_preference()` runs instead, and it is
also **per sentence**. The reason is specific:

> *"v4 produced 'This is a general suggestion as you have not chosen any preferences.
> It is Continental and vegetarian, matching those you set.' — a correct denial
> followed by a false claim, so a whole-text denial check would have waved it through."*

An explicit denial ("you have not chosen any preferences", "matching none of your
preferences") is correct and must survive.

### 16.8 The deterministic fallback

`deterministic_explanation(request)` builds a sentence from the request's own fields.
It states what the item is and, for the dimensions the customer **actually set**,
whether they line up. **Unset dimensions are simply not discussed** — which is exactly
the behaviour the model could not be taught reliably.

Prose tables (`_SPICE_MATCHED`, `_SPICE_BARE`, `_cuisine_word`) make it read like
English rather than like a database row:

| Situation | Output |
| --- | --- |
| All three match | *"Paneer Tikka is Indian, vegetarian and hot, matching what you asked for."* |
| Two match, one differs | *"Paneer Tikka is vegetarian and hot, matching what you asked for. However, it is Indian rather than Thai."* |
| Nothing set | *"Black Bean Tacos is a vegan Mexican dish with a medium spice level. It is shown as a general suggestion."* |

One subtlety worth preserving: when the dietary types differ, the fallback says *"it is
vegan, which still suits your vegetarian preference"* rather than reporting a conflict —
because the engine's dietary filter **already guaranteed compatibility**, so this is a
difference, never a conflict.

**Why the fallback is safe:** it has no price, no ingredient, no availability flag and
no history to draw on. It cannot invent them because it was never given them.

### 16.9 Measured results

From `docs/AI.md` §18.3 — 25 held-out cases, the same methodology used for the earlier
adapters:

| System | raw | **+ guard** | coverage | concise | avg words |
| --- | ---: | ---: | ---: | ---: | ---: |
| base | 17/25 | 17/25 | 5/19 | 25/25 | 33.0 |
| v2 | 16/25 | 19/25 | 14/19 | 25/25 | 27.1 |
| v3 | 22/25 | 24/25 | 18/19 | 25/25 | 18.4 |
| **v4** | **23/25** | **25/25** | **19/19** | **25/25** | **17.7** |

Failure taxonomy with the guard applied:

| Failure class | base | v2 | v3 | **v4** |
| --- | ---: | ---: | ---: | ---: |
| HALLUCINATION | 6 | 3 | 0 | **0** |
| OVERCLAIM | 0 | 3 | 0 | **0** |
| LABEL_FACT_CONTRADICTION | 1 | 1 | 0 | **0** |
| DIETARY_COMPATIBILITY_ERROR | 0 | 1 | 0 | **0** |
| CROSS_DIMENSION_ERROR | 0 | 0 | 0 | **0** |
| GARBLED_PROSE | 1 | 1 | 0 | **0** |
| NOT_SET_ERROR | 0 | 0 | 1 | **0** |

**Contrastive tracking: v4 scored 9/9.**

Validation of the guard itself (§18.5):

| Check | Result |
| --- | --- |
| False positives on v4's 240 hand-authored outputs | **0** |
| False positives on v3's 200 | **0** |
| Known failures from M07.3/M07.6 across v2/v3/v4 | **all caught** |
| Fallback safe under its own check (756 combinations) | **0 unsafe** |
| Fallback within 400 chars and 1–2 sentences | **0 violations** |
| Guard firing rate on v4 | **2/25** |

The guard fired on only 2 of 25 v4 generations, so it is a narrow correction rather
than a blanket rewrite. It is also model-agnostic and improves every adapter
(v2 16→19, v3 22→24) — but **v2 with the guard still scores only 19/25** and retains 3
hallucinations, 3 overclaims and a dietary error. *"That is why V4, not the guard
alone, is what makes production safe."*

`tests/test_preference_safeguard.py` pins all of it with **60 permanent tests**.

### 16.10 ⚠ What this safeguard is not

> **This is a domain-specific safeguard for Quick Junction recommendation explanations.
> It is not general-purpose AI safety, it is not a hallucination detector, and it does
> not make the model trustworthy.**

Its scope is exactly one thing: *does this sentence credit the customer with a
preference they did not set?* Everything else is out of scope by design.

`docs/AI.md` §18.6 states the limits honestly, and you should know them:

* **It is regex-based.** Precise on the phrasings four model generations actually
  produced, and provably clean on 440 authored outputs — *"but it is pattern matching
  and a novel paraphrase could slip past. The live test finding `unset` is the proof of
  that, and the reason the fallback is the safety net rather than the detector."*
* **It only polices *unset* preferences.** A misstatement about a preference the
  customer *did* set is outside its remit. The docs record one such case: *"it once
  wrote 'It is vegetarian, which is what you asked for' to a customer who asked for
  non-vegetarian."*
* **25 held-out cases plus 12 live scenarios is a behavioural probe, not a statistic.**

**A worked example of the second limitation**, observed during the 2026-08-20 audit
with preferences Mexican / Vegetarian / Mild:

> Model output: *"This is Mexican and mild, but it is vegan rather than vegetarian."*
> The dish (Black Bean Tacos) is **medium**, not mild — and the fact panel beside it
> correctly reads *"Spice level — Medium — you prefer Mild."*

All three preferences were set, so the guard does not apply and the sentence passes.
This is the documented gap, working exactly as documented. **The mitigation is the
two-panel layout**: the authoritative facts sit next to the prose, labelled, so the
reader can check.

### 16.11 🔒 Do not bypass the safeguard

> `LLM_ADAPTER_PATH` selects the model. **There is no configuration switch that
> disables the safeguard, and none should be added.**
>
> V4 was promoted *conditionally* on it. They ship together, and
> `tests/test_dataset_v4.py::test_6` pins that pairing. Removing or weakening the guard
> takes the production model from 25/25 to 23/25 and reopens a failure class that
> three dataset iterations could not close.

---

## 17 — Recommendation vs AI — the comparison

**If you take one table from this document, take this one.**

| | **Recommendation Engine** | **Local AI** | **Safeguard** |
| --- | --- | --- | --- |
| **File** | `app/services/recommendations.py` | `app/services/local_llm.py` | `app/services/local_llm.py` |
| **Purpose** | **Choose** the recommendation | **Explain** the recommendation | **Prevent** unsupported preference claims |
| **Technology** | TF-IDF + cosine similarity (scikit-learn) | Qwen3-0.6B-Base + V4 LoRA (PyTorch/PEFT) | Plain Python regex + comparison |
| **Deterministic?** | **Yes** — no randomness, stable tie-break on name; same inputs always give the same ranking | **The decoding is greedy** (`do_sample=False`), so the same facts give the same sentence with the same model. But it is a neural network: change the adapter or the library versions and the wording changes. **Its content is not guaranteed** | **Yes** — pure function of the text and the request |
| **Authoritative?** | **Yes.** It decides what a customer may see | **No.** Advisory only | Yes, over the explanation |
| **Required for the app to work?** | **Yes** | **No** | Runs whenever the AI does |
| **Can state a price?** | Reads real prices from `MenuItem` rows | **No** — never given one | No |
| **Speed** | 16–62 ms | 6.1–6.3 s warm | microseconds |
| **Fails how?** | Returns an empty list | Returns `None` | Returns a deterministic replacement |

### 17.1 Why the AI does not decide the recommendation

Four reasons, all of which the code enforces rather than merely intends:

1. **Auditability.** A deterministic ranking can be explained token by token. A neural
   ranking cannot.
2. **Safety.** The dietary filter is a hard rule. A model that "usually" respects a
   dietary restriction is not acceptable — it must be structurally impossible to
   return an excluded item, and it is, because the filter runs before the model exists
   in the flow.
3. **Availability.** The application must work with no model files. If the model chose
   items, there would be nothing to fall back to.
4. **Speed.** Ranking must be instant. Six seconds per page load is not a product.

### 17.2 The one-sentence version

> **The engine decides. The model describes. The safeguard checks the description
> against the truth. The facts are shown beside the prose so a human can check too.**

---

## 18 — AI failure and no-model behaviour

### 18.1 The three failure cases

| Case | What happens |
| --- | --- |
| **`models/` does not exist** | `_load()` finds no directory, logs `Local LLM disabled: model directory not found at <path>`, sets `_load_failed`, returns `(None, None)`. Every later call returns immediately |
| **The V4 adapter cannot load** | The base model has already loaded successfully. PEFT raises; the handler logs `Could not load LoRA adapter; continuing with base model` and **keeps the base model**. Explanations still appear, but quality is noticeably worse |
| **Generation fails** | The handler logs `Local LLM generation failed` and returns `None` |

### 18.2 What the user sees

`/recommendations/explain` renders both panels regardless. When `explanation is None`,
the AI panel shows:

> **AI explanation** · *Advisory*
>
> AI explanation temporarily unavailable. The recommendation itself is unaffected — it
> is calculated without the model.

The deterministic fact panel beside it — item, category, price, cuisine, dietary type,
spice level, match label — is **fully rendered**. The page still returns **200**.

### 18.3 Verified, not assumed

During the 2026-08-20 audit, a second application instance was started with
`LLM_MODEL_PATH` pointing at a directory that does not exist. Result:

| Route | Status |
| --- | --- |
| `/` | 200 |
| `/menu` | 200 |
| `/recommendations` | 200 |
| `/recommendations/explain` | 200, with the "temporarily unavailable" panel |

**No traceback appeared in any response body.** The real model directory was never
moved.

`tests/test_llm.py` tests 9 and 9b assert the same thing permanently: the page still
returns 200 with the real item and price when the model yields nothing, and
`/recommendations` is entirely unaffected.

### 18.4 What still works without any model

**Everything except one panel on one page.**

| Feature | Works without the model? |
| --- | --- |
| Landing page, menu browsing, item detail | ✅ |
| Registration, login, logout, sessions | ✅ |
| Preferences | ✅ |
| **Recommendations and ranking** | ✅ — the engine never uses the model |
| Cart, checkout, order history | ✅ |
| Staff order queue and status workflow | ✅ |
| Admin category and menu management | ✅ |
| Audit logging | ✅ |
| **The AI explanation sentence** | ❌ — replaced by a notice |

### 18.5 The design principle

From the module docstring:

> *"Failure is expected and non-fatal."*

And from `HANDOFF.md`:

> *"**A missing model is never a crash.**"*

**The AI is additive by construction.** That is why `requirements-ai.txt` is a separate
file, why `LLM_ENABLED` exists, and why every entry point returns `None` instead of
raising.

---

## 19 — Dataset and training history

> ## ⛔ Do not perform training.
>
> **The project is currently in the product / demo / handover phase. No further model
> training is required for the current release.** `HANDOFF.md` states it plainly:
> *"This is the final selected adapter; no further training is planned or required."*
>
> This section documents the historical pipeline so you understand where the production
> adapter came from. It is not an instruction to run any of it.

### 19.1 The four dataset versions

All four are **hand-authored for this project** — not scraped, not downloaded, not
generated by another model.

| Version | File | Examples | Train / Val | Categories | Status |
| --- | --- | --- | --- | --- | --- |
| v1 | `data/raw/seed_examples.jsonl` | 60 | 50 / 10 | 10 | Historical |
| v2 | `data/raw/seed_examples_v2.jsonl` | 150 | 120 / 30 | 15 | Historical |
| v3 | `data/raw/seed_examples_v3.jsonl` | 200 | 161 / 39 | 13 | Historical |
| **v4** | `data/raw/seed_examples_v4.jsonl` | **240** | **195 / 45** | 15 | ✅ **PRODUCTION** |

Format is JSONL: `category`, `instruction`, `input`, `output`, plus an optional
`group_id` in v3 and v4 for contrastive grouping.

### 19.2 Why each version exists

| Version | The problem it targeted |
| --- | --- |
| **v1** | First attempt. 60 examples — enough to tune style, not knowledge |
| **v2** | Rebalanced 60→150. Added partial / weak / mismatch and contradiction-correction categories to fix the M07 sycophancy defect. Evaluation improved v1 5/8 → v2 8/8 |
| **v3** | Fixed a **prompt-shape defect**: only 13 % of v2's examples were in the exact shape production emits. Every v3 example is |
| **v4** | Targeted the residual defect — crediting the customer with preferences they never set. 240 examples across 15 categories under four explicit authoring rules |

### 19.3 The split is deterministic

`scripts/build_dataset.py` hashes each example (SHA-256 over its canonical JSON),
orders examples by that hash within their category, and sends a fixed fraction of each
category to validation.

> *"No RNG and no seed to drift — re-running on unchanged input reproduces
> byte-identical output, and adding an example never reshuffles the ones already
> placed."*

Splitting *within* category keeps every category represented on both sides.

> **⚠ Gotcha:** `scripts/build_dataset.py` defaults to **`--version v2`**, not v4. The
> script's own docstring flags this: *"`DEFAULT_VERSION` below is deliberately left at
> `v2`: it is the value M07.1 shipped, nothing in the running application depends on
> it, and changing it would silently alter what a bare invocation rebuilds. Pass
> `--version` explicitly."*
>
> `training/train_lora.py` is separate and **does** default to `v4`, matching
> production.
>
> **Running the builder is not part of setting the project up.** The processed splits
> are committed, and the production adapter is a finished artifact that is copied in
> rather than rebuilt.

### 19.4 The V4 training run

From `models/qwen3-0.6b-quickjunction-lora-v4/training_metrics.json` — the actual
recorded run, not a plan:

```json
{
  "dataset_version": "v4",
  "base_model": "...\\models\\Qwen3-0.6B-Base",
  "learning_rate": 0.0002,
  "batch_size": 1,
  "grad_accum": 4,
  "train_runtime_seconds": 3693.97,
  "train_loss": 0.5306,
  "steps": 147,
  "train_examples": 195,
  "validation_examples": 45,
  "lora_rank": 8,
  "trainable_parameters": 5046272,
  "total_parameters": 601096192,
  "trainable_percent": 0.8395,
  "device": "cpu",
  "smoke_run": false,
  "epochs": 3.0
}
```

**About 62 minutes on CPU.** No GPU.

> **The loss number proves nothing about quality.** `docs/AI.md` §16.4 makes this point
> explicitly. A training loss of 0.53 says the model fits its training data; it says
> nothing about whether the sentences are true. That is what the held-out evaluation in
> §16.9 is for.

### 19.5 Why V4 is production

Two things had to be true together:

1. **V4 scored best of the four** on the 25-case held-out evaluation: 23/25 raw, and
   **25/25 with the safeguard**, with every failure class at zero and contrastive
   tracking at 9/9.
2. **The safeguard closed the one class V4 could not.** Promotion was *conditional* on
   it. `docs/AI.md`: *"the two ship together and are pinned together by
   `tests/test_dataset_v4.py::test_6`."*

### 19.6 Where the evidence lives

| Artifact | Location |
| --- | --- |
| Full milestone-by-milestone record | `docs/AI.md` §§11–18 |
| The datasets | `data/raw/`, `data/processed/` |
| Dataset documentation | `data/README.md` |
| Training harness | `training/train_lora.py` |
| Evaluation harnesses | `training/evaluate.py`, `evaluate_production.py`, `eval_scenarios_production.py` |
| Recorded run metadata | `models/qwen3-0.6b-quickjunction-lora-v4/training_metrics.json` |
| Permanent dataset tests | `tests/test_dataset_v2.py`, `test_dataset_v3.py`, `test_dataset_v4.py` |
| Permanent safeguard tests | `tests/test_preference_safeguard.py` (60 tests) |

### 19.7 Runtime vs historical — the file-level split

| Loaded when the app runs | Never imported by the app |
| --- | --- |
| `app/services/local_llm.py` | `training/train_lora.py` |
| `models/Qwen3-0.6B-Base/` | `training/evaluate*.py` |
| `models/qwen3-0.6b-quickjunction-lora-v4/` | `scripts/build_dataset.py` |
| | `data/` (entirely) |
| | `models/...-lora`, `-v2`, `-v3` |

You could delete the right-hand column and the application would run unchanged. **Do
not** — it is the evidence for the project's central technical claim.

### 19.8 If a future developer genuinely needs to retrain

Not now, and not for this release. But for completeness, `docs/AI.md` §10 documents the
reproduction path, and `training/train_lora.py --smoke --max-steps 2` is a ~1-minute
pipeline check that trains nothing meaningful.

**Before ever doing so:** read `docs/AI.md` §§15–18 in full, keep the existing V4
adapter untouched, write the new adapter to a **new directory**, and evaluate against
the same 25 held-out cases before changing `LLM_ADAPTER_PATH`.

---

## 20 — Configuration

`config.py` — 8 KB, four classes, one validation contract.

### 20.1 The class hierarchy

```text
BaseConfig                shared settings + validate()
   ├── DevelopmentConfig  DEBUG on, insecure cookie, warm-up on
   ├── TestingConfig      SQLite, CSRF off, LLM off, no file logging
   └── ProductionConfig   stricter validate(): key strength, no debug, no SQLite
```

`get_config(name)` resolves a name to a class, falling back to `APP_ENV`, then to
`"development"`. An unknown name raises `ConfigError`.

### 20.2 `BaseConfig` — the shared settings

| Setting | Value | Why |
| --- | --- | --- |
| `SECRET_KEY` | from env | Signs the session cookie |
| `SQLALCHEMY_DATABASE_URI` | from `DATABASE_URL` | **No default** — unset fails immediately, by design |
| `SQLALCHEMY_ENGINE_OPTIONS` | `pool_pre_ping=True`, `pool_recycle=280` | *"MySQL drops idle connections; recycle below the server's `wait_timeout`"* |
| `SESSION_COOKIE_NAME` | `qj_session` | |
| `SESSION_COOKIE_HTTPONLY` | `True` | JavaScript cannot read the cookie |
| `SESSION_COOKIE_SAMESITE` | `Lax` | Cross-site CSRF mitigation |
| `SESSION_COOKIE_SECURE` | `True` | Relaxed **only** in development |
| `PERMANENT_SESSION_LIFETIME` | 8 hours | |
| `MAX_CONTENT_LENGTH` | 2 MiB | Caps request body size |
| `WTF_CSRF_ENABLED` | `True` | |
| `WTF_CSRF_TIME_LIMIT` | `None` | Tokens do not expire independently of the session |
| `PROPAGATE_EXCEPTIONS` | `False` | *"Never re-raise handled exceptions to the client"* |
| `LOG_LEVEL` | `INFO` | |
| `LOG_DIR` | `<repo>/logs` | |
| `LOG_TO_FILE` | `True` | |

### 20.3 The three environments compared

| | development | testing | production |
| --- | --- | --- | --- |
| `DEBUG` | `True` | `False` | `False`, enforced |
| `SESSION_COOKIE_SECURE` | **`False`** | `False` | `True` |
| `LOG_LEVEL` | `DEBUG` | `WARNING` | `INFO` |
| `LOG_TO_FILE` | `True` | **`False`** | `True` |
| `WTF_CSRF_ENABLED` | `True` | **`False`** | `True` |
| Database | MySQL | **in-memory SQLite** | MySQL, SQLite rejected |
| `LLM_ENABLED` | `True` | **`False`** | `True` |
| `LLM_WARMUP` | **`True`** | n/a | **`False`** |
| `SECRET_KEY` check | none | fixed test value | ≥32 chars, not a placeholder |

`SESSION_COOKIE_SECURE = False` in development carries the reason inline: *"Plain HTTP
on localhost, so the Secure flag would drop the cookie."*

`TestingConfig.LLM_ENABLED = False` also carries its reason: *"The 1.2 GB model must
never be loaded by the ordinary test suite — tests that exercise inference opt in
explicitly by flipping this."*

### 20.4 Validation

`BaseConfig.validate()` requires `SECRET_KEY` and `SQLALCHEMY_DATABASE_URI`, and
produces a message naming the **environment variable** rather than the config attribute:

```text
ProductionConfig: missing required environment variable(s): DATABASE_URL.
Copy .env.example to .env and fill it in.
```

`ProductionConfig.validate()` adds three checks:

```python
if cls.DEBUG or cls.TESTING:
    raise ConfigError("Debug/testing mode is forbidden in production.")
if cls.SECRET_KEY in _WEAK_SECRETS or len(cls.SECRET_KEY or "") < 32:
    raise ConfigError("SECRET_KEY is weak or a placeholder. ...")
if uri.startswith("sqlite"):
    raise ConfigError("Production requires the MySQL DATABASE_URL.")
```

`_WEAK_SECRETS` includes `None`, `""`, `change-me`, `changeme`, `secret`, `dev`,
`development`, `testing-only-not-a-secret`, and the literal placeholder from
`.env.example`.

### 20.5 LLM configuration

Covered in §13.2. The comments around `LLM_ADAPTER_PATH` are worth reading in the
source — they record *why* v4 was promoted and why the safeguard is not optional.

### 20.6 What to change, and what not to

| Setting | Safe to change? |
| --- | --- |
| `LOG_LEVEL` | ✅ Yes |
| `FLASK_RUN_HOST` / `FLASK_RUN_PORT` | ✅ Yes, locally |
| `LLM_MAX_NEW_TOKENS` | ⚠️ Yes, but it trades explanation length against latency |
| `LLM_WARMUP` | ⚠️ In production, only after doing the memory arithmetic (§14.4) |
| `LLM_ENABLED` | ⚠️ `false` disables the AI entirely; the app still works |
| `LLM_ADAPTER_PATH` | 🔴 **No.** v4 is production. Changing this changes the product's behaviour |
| `SESSION_COOKIE_*` | 🔴 **No.** These are security settings |
| `PROPAGATE_EXCEPTIONS` | 🔴 **No.** `True` leaks tracebacks to clients |
| `ProductionConfig.validate()` | 🔴 **No.** It exists to stop unsafe deployments |
| `SQLALCHEMY_ENGINE_OPTIONS` | 🔴 Not without understanding MySQL's `wait_timeout` |

---

## 21 — Logging and errors

### 21.1 Two logging channels

`app/utils/logging.py` configures two, deliberately separate:

| Logger | File | Contains |
| --- | --- | --- |
| `app.logger` | `logs/app.log` | General application events, model loading, unhandled exceptions |
| `quickjunction.security` | `logs/security.log` | Authentication, authorization and abuse events |

> *"written to a separate file so it can be shipped to a SIEM or alerted on
> independently."*

The security logger sets `propagate = False`, so its records do not also land in
`app.log`.

Both use a rotating handler: **2 MB per file, 5 backups**. The security file handler is
pinned at `INFO` regardless of `LOG_LEVEL`, so security events are never silenced by
turning down general verbosity.

Format:

```text
%(asctime)s %(levelname)-8s [%(name)s] %(message)s
```

### 21.2 The logging rule

> *"Nothing logged here may contain a secret, a password, a session token or a full
> database URI. **Log identifiers, never credentials.**"*

Actual security-log lines look like this:

```text
2026-08-20 11:56:26,560 INFO     [quickjunction.security] login_success user_id=1 ip=127.0.0.1
2026-08-20 11:59:09,060 WARNING  [quickjunction.security] login_failure user_id=None ip=127.0.0.1
```

An identifier and an IP. No username on success, no password ever.

### 21.3 AI failure logging

Three distinct messages, each meaning something different:

| Message | Meaning |
| --- | --- |
| `Local LLM disabled: model directory not found at <path>` | `models/` missing or the path is wrong |
| `Could not load LoRA adapter; continuing with base model` | **Running the base model** — see §13.7 |
| `Local LLM unavailable; explanations disabled` | Dependency, memory or corrupt-weights failure |
| `Local LLM generation failed` | Generation itself raised |
| `Explanation rejected: it credited a preference the customer did not set` | **The safeguard fired.** Informational, not an error |

`docs/AI.md` §8 records the privacy rule for this channel: *"Logging records only that
inference failed — never the prompt, the preferences, or the generated text."*

### 21.4 Error handling

`app/utils/errors.py` registers two handlers:

```python
@app.errorhandler(HTTPException)
def handle_http_exception(exc):
    return render_error(exc.code or 500, exc.description or exc.name)

@app.errorhandler(Exception)
def handle_unexpected(exc):
    app.logger.exception("Unhandled exception: %s", type(exc).__name__)
    return render_error(500, "An internal error occurred.")
```

**Full detail to the log, a generic message to the client.** The comment adds the rule
that keeps the two response formats aligned: *"The HTML page shows the same generic
sentence — switching format must never widen what is disclosed."*

### 21.5 HTML versus JSON errors

`render_error(status, message)` calls `wants_html()` (§5.8) and dispatches:

* **Browsers** get a styled page extending `base.html` — `errors/403.html`,
  `errors/404.html`, `errors/500.html`, or `errors/generic.html` for anything else
  (a 405 or 429 still renders as a product page).
* **API clients** get:
  ```json
  {"error": {"status": 500, "message": "An internal error occurred."}}
  ```

### 21.6 Verified: no disclosure even with the database down

During the 2026-08-20 audit an instance was started against an unreachable MySQL port.
Data-backed pages returned a styled 500:

> **Something went wrong on our side.** The problem has been recorded. Nothing you did
> caused it, and your order history and cart are unaffected. Please try again in a
> moment.

**No DSN, no password, no driver name and no traceback appeared in any response.**
JSON clients received the generic body above.

Note that `/health` still returned **200**. That is intentional — it is documented as a
liveness probe that *"deliberately reveals nothing: no version, no environment name, no
database state, no host details."*

---

## 22 — Security architecture

> **These are the security controls currently implemented and tested in the project.**
> This is not a claim that the application is secure against all attacks, and §22.3
> lists what is deliberately still missing. `docs/SECURITY.md` is the full treatment.

### 22.1 Implemented controls

| Control | Implementation | Verified |
| --- | --- | --- |
| **Password hashing** | Argon2id via argon2-cffi with library defaults, which target OWASP-recommended parameters and self-upgrade | `tests/test_auth.py` |
| **Password policy** | ≥10 chars, ≤128, must contain a letter and a digit, small breached-password deny-list | `tests/test_auth.py` |
| **CSRF** | `CSRFProtect` application-wide; every state-changing form carries a token | POST without a token → **400**, audit 2026-08-20 |
| **Authentication** | Server-side session holding only `user_id` and `sv`; user re-loaded from the database every request | |
| **Session revocation** | `User.session_version` incremented on logout invalidates every previously issued cookie | `tests/test_session_revocation.py` (18 tests) |
| **Session fixation** | `session.clear()` before establishing a new session, on login **and** registration | |
| **Role authorization** | `@require_role` checked server-side on every request; navbar hiding is cosmetic | Customer → **403** on all seven staff/admin routes, audit 2026-08-20 |
| **IDOR protection** | Ownership enforced *inside the query*: `filter_by(id=..., user_id=...)`. No `user_id` parameter exists in any customer route | Other users' orders → **404**, own order → **200**, audit 2026-08-20 |
| **User enumeration** | Identical "Invalid username or password." for a wrong password and an unknown user | Verified identical, audit 2026-08-20 |
| **Rate limiting** | 5 login attempts per 15 minutes, in-process | `tests/test_auth.py` |
| **Open-redirect protection** | `safe_next_target()` — see §5.9 | `tests/test_demo_polish.py` |
| **Order status transitions** | Server-side allow-list; UI renders only legal buttons; service re-checks regardless of what was posted | `tests/test_staff_orders.py` |
| **Server-side pricing** | Every price re-read from `menu_items` inside the checkout transaction. Client-submitted prices never accepted | `tests/test_orders.py` |
| **SQL injection** | SQLAlchemy parameterises every query. **No raw SQL string interpolation anywhere in `app/`** | |
| **XSS** | Jinja2 autoescaping on; **no template uses `\|safe`** — including for model output | |
| **Secret handling** | `.env` git-ignored; production rejects weak/placeholder `SECRET_KEY`, debug mode, and SQLite | |
| **Safe error responses** | Generic message to the client, full detail to the log, in both HTML and JSON | Verified with the database down, audit 2026-08-20 |
| **Prompt injection** | `_sanitise()` strips structural characters including colons; caps free text at 120 chars | `tests/test_llm.py::test_12` |
| **No network from the model** | Offline env flags, `local_files_only=True`, no HTTP client, no API key | A test greps the module for network/API identifiers |
| **safetensors, never pickle** | Both the base weights and the adapter | |
| **Audit trail** | 17 event types, append-only, one writer. Never a password, hash, token or request body | `tests/test_auth.py`, `test_orders.py`, `test_staff_orders.py` |
| **Request size cap** | `MAX_CONTENT_LENGTH = 2 MiB` | |
| **Enum allow-lists at the database** | `VARCHAR` + `CHECK` on six columns | `tests/test_migrations.py` |

### 22.2 The trust boundaries

```text
UNTRUSTED
  Browser input  ── form fields, query strings, headers, the session cart's contents
  Model output   ── never executed, never interpolated into SQL, never rendered unescaped
  Menu free text ── staff-authored; sanitised before it enters the prompt

TRUSTED
  The session's user_id ── signed, and re-validated against the database each request
  Database rows         ── the source of truth for prices, availability, roles
  Server-side config    ── model paths, feature switches
```

**The single most important boundary:** the cart is untrusted data in a signed cookie,
and **no price ever comes from it**. Prices are re-read from the database on every cart
render and again inside the checkout transaction.

### 22.3 What is deliberately not implemented

From `docs/SECURITY.md` §17, ranked there by how much the absence costs:

| Missing | Needed by |
| --- | --- |
| Payment gateway / card processing | The first real transaction |
| Multi-worker-safe rate limiting (Flask-Limiter + Redis) | The first multi-worker deployment |
| Security headers — HSTS, `X-Content-Type-Options`, `X-Frame-Options`, CSP | The first HTML page shipped to real users |
| Stripping the `Server` header | Deployment |
| TLS termination and HTTP→HTTPS redirect | Deployment |
| A least-privilege MySQL user — no `GRANT ALL` | Deployment |
| Password reset flow | The first locked-out real user |
| Email verification at registration | Before email is trusted for anything |
| Login-time rehash (`needs_rehash()` is written but never called) | The first Argon2 parameter change |
| A maintained breached-password feed | Before it matters for a real user base |
| Audit-log retention/purge enforced in code | The first real data or compliance review |
| Encrypted, tested database backups | The first real data |
| Dependency scanning (`pip-audit`) in CI | The first CI run |
| Customer-initiated order cancellation and its downstream handling | The first customer who needs to cancel |
| Cart persistence across login | The first user who builds a cart before signing in |
| Multi-worker-safe checkout idempotency | The first multi-worker deployment |

**Naming these is a strength, not a weakness.** A handover that pretends they do not
exist is worse than one that lists them with the stage each becomes necessary.

### 22.4 The AI-specific security position

`docs/SECURITY.md` §17's AI subsection records the current state of each original rule:

* Prompt injection → **addressed** (input sanitised, output never parsed or executed)
* No customer PII to the model → **held** (menu attributes and enum values only)
* The model advises, never authorises → **held** (no price, no id, cannot write)
* Local inference only → **held** (offline flags, `local_files_only`, no HTTP client)
* safetensors, never pickle → **held**

The standing policy, retained:

> *"**Prompt injection is an input-validation problem.** Model output is untrusted
> text: it may never be executed, never be interpolated into SQL, and never be rendered
> unescaped."*

---

## 23 — Testing architecture

### 23.1 Running the suite

```powershell
pytest -q
```

`pytest.ini`:

```ini
[pytest]
testpaths = tests
pythonpath = .
addopts = -q
```

### 23.2 The current result

Measured on **2026-08-20** at commit `912150a`, taken from a JUnit XML report produced
by the run itself:

```text
tests="420"  failures="0"  errors="0"  skipped="0"   time≈29.4s
12 warnings
```

The 12 warnings are all the same `DeprecationWarning` about `get_engine` in
`migrations/env.py`, raised by `tests/test_migrations.py`. Harmless.

> ### ⚠ Documentation drift — reconcile before quoting a number
>
> Other documents in this repository quote different, **stale** counts:
>
> | Source | Claims |
> | --- | --- |
> | `README.md` §12 | 221 tests |
> | `HANDOFF.md` | 378 tests |
> | **Measured at commit `912150a`** | **420 tests, 0 skipped** |
>
> The 420 figure is the one to trust. The gap is mostly `tests/test_demo_polish.py`,
> which contributes 42 tests and was added in the final polish commit.

### 23.3 Test count by module

| Module | Tests | Covers |
| --- | ---: | --- |
| `test_preference_safeguard.py` | **60** | The safeguard — rejection *and* survival cases, placeholder paraphrases, every partial preference combination, an exhaustive fallback sweep |
| `test_demo_polish.py` | 42 | Landing page, error pages, `next` handling, loading indicator, demo readiness |
| `test_llm.py` | 32 | Prompt construction, sanitisation, offline enforcement, fallback behaviour |
| `test_dataset_v3.py` | 31 | V3 dataset structure and validation |
| `test_auth.py` | 29 | Registration, login, password policy, rate limiting, enumeration |
| `test_orders.py` | 28 | Cart, checkout, server-side pricing, order history, IDOR |
| `test_dataset_v4.py` | 26 | V4 dataset validation, and the V4-plus-safeguard pinning |
| `test_menu.py` | 26 | Menu browsing, admin CRUD, validation |
| `test_recommendations.py` | 26 | TF-IDF ranking, dietary filter, history weighting, determinism |
| `test_ui.py` | 24 | Template rendering, navigation, role-aware UI |
| `test_dataset_v2.py` | 22 | V2 dataset structure |
| `test_evaluation_production.py` | 22 | The production evaluation harness |
| `test_staff_orders.py` | 20 | The order queue and the status state machine |
| `test_session_revocation.py` | 18 | Session versioning and logout revocation |
| `test_foundation.py` | 10 | App factory, configuration, `/health` |
| `test_migrations.py` | 4 | The migration chain applies cleanly |
| **Total** | **420** | |

### 23.4 How the tests are isolated

`tests/conftest.py`:

```python
@pytest.fixture
def app() -> Flask:
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()
```

Consequences worth knowing:

* **No MySQL server needed.** `TestingConfig` uses in-memory SQLite.
* **No model files needed.** `TestingConfig.LLM_ENABLED = False`, and `warm_up()`
  returns early when `TESTING` is set.
* **No real data is touched.** Each test gets a fresh schema and drops it afterwards.
* **CSRF is disabled** in testing so tests can POST without scraping a token — though
  `conftest.py` also provides regexes to extract one where a test wants to exercise the
  real path.
* **Foreign keys are enforced on SQLite** by the `PRAGMA foreign_keys=ON` listener in
  `app/extensions.py`, so the tests do not pass on constraints MySQL would enforce
  differently.

That combination is why `pytest -q` is a good **first** check on a new machine — it
works before MySQL or `models/` are set up at all.

### 23.5 About skips

Five call sites across `test_dataset_v2.py`, `test_dataset_v3.py` and
`test_dataset_v4.py` call `pytest.skip(...)` when an adapter directory is absent:

```python
pytest.skip("V4 adapter not present in this checkout (models/ is git-ignored)")
```

**On a machine that has `models/`, nothing skips** — which is why the measured run above
reports `skipped="0"`.

**On a clean checkout without the weights**, those adapter-dependent tests skip **by
design** and the run still passes. A skip there is expected behaviour, not a failure:
`models/` is git-ignored, so a fresh clone genuinely has no adapter to inspect, and the
suite is built to stay green in that state.

> Other documentation in this repository refers to "4 intentional historical-adapter
> skips". The mechanism described above is real; the exact number depends on which
> adapter directories are present. **Do not quote a fixed skip count** — run the suite
> and read the actual result.

### 23.6 Rules for changing tests

| | |
| --- | --- |
| ✅ | Add tests for new behaviour |
| ✅ | Add a regression test with every bug fix |
| ✅ | Update a test when behaviour changes **intentionally**, and say so in the commit |
| 🔴 | **Never** weaken or delete a test to make a suite green |
| 🔴 | **Never** loosen `tests/test_preference_safeguard.py` — those 60 tests are what stops the safeguard being quietly eroded |
| 🔴 | **Never** remove `tests/test_dataset_v4.py::test_6` — it pins V4 to the safeguard |

---

## 24 — Important files map

| File | What it controls | When to modify |
| --- | --- | --- |
| **`run.py`** | Development entry point; host, port, debug | Almost never |
| **`config.py`** | Every configuration value and the startup validation contract | Adding a new setting. **Read §20.6 first** |
| **`app/__init__.py`** | `create_app()` — assembly order, extensions, blueprints, context processor, AI warm-up | Adding an extension or a global template value |
| **`app/extensions.py`** | Unbound `db`, `migrate`, `csrf`; the SQLite FK listener | Adding a Flask extension |
| **`app/routes/__init__.py`** | Blueprint registration | Adding a blueprint |
| `app/routes/auth.py` | Register, login, logout, `safe_next_target()` | Changing the authentication flow |
| `app/routes/preferences.py` | `/preferences`, `/recommendations`, `/recommendations/explain` | Changing the recommendation or explanation pages |
| `app/routes/cart.py` | Cart add / update / remove / clear | Changing cart behaviour |
| `app/routes/orders.py` | Checkout and customer order history | Changing checkout |
| `app/routes/staff_orders.py` | Order queue and status updates | Changing the staff workflow |
| `app/routes/admin_menu.py` | Category and menu-item CRUD | Changing admin management |
| `app/routes/forms.py` | **Every WTForms form** | Adding or changing a form field |
| **`app/services/recommendations.py`** | **The ranking algorithm and the dietary hard filter** | Changing how items are chosen. High blast radius |
| **`app/services/local_llm.py`** | **Model loading, prompt, generation, the safeguard, warm-up** | Changing AI behaviour. **Read §16 first** |
| `app/services/orders.py` | Checkout transaction, `ALLOWED_STATUS_TRANSITIONS` | Changing pricing or the status workflow |
| `app/services/cart.py` | Cart validation and live-menu resolution | Changing quantity limits |
| `app/services/menu.py` | Menu/category validation and CRUD | Changing menu validation |
| `app/services/preferences.py` | Preference read/write and coercion | Adding a preference dimension |
| `app/services/auth.py` | `register_user`, `authenticate_user` | Changing registration rules |
| `app/services/audit.py` | The **only** audit-log writer | Adding an audited event |
| `app/services/errors.py` | `ValidationError` | Almost never |
| **`app/models/enums.py`** | `SpiceLevel`, `DietaryType`, `Cuisine`, and `enum_column()` | Adding an enum value — **needs a migration** |
| `app/models/user.py` | `User`, `Role`, `session_version` | Changing the account schema |
| `app/models/menu_item.py` | `MenuItem`, price constraint, indexes | Changing the menu schema |
| `app/models/order.py` | `Order`, `OrderItem`, `OrderStatus`, snapshot columns | Changing the order schema |
| `app/models/customer_preference.py` | The three nullable preference columns | Adding a preference dimension |
| `app/models/audit_log.py` | `AuditLog`, `AuditEvent` (17 values) | Adding an event type — **needs a migration** |
| **`app/utils/authorization.py`** | `login_required`, `require_role`, session handling, revocation | Changing authentication or authorization. **Security-critical** |
| `app/utils/security.py` | Argon2 hashing, password policy, normalisation | Changing the password policy |
| `app/utils/errors.py` | Content negotiation and error handlers | Changing error presentation |
| `app/utils/formatting.py` | The `money` filter and `CURRENCY_SYMBOL` | Changing the currency |
| `app/utils/logging.py` | Both logging channels, rotation | Changing logging |
| `app/utils/cart.py` | Session cart read/write | Rarely |
| `app/utils/ratelimit.py` | 5 attempts / 15 minutes | Changing limits, or swapping in Redis |
| **`app/templates/base.html`** | Layout, navbar, flash area, footer, script tags | Any global UI change |
| `app/templates/partials/_forms.html` | Form field macros and the spinner submit button | Changing form rendering everywhere at once |
| `app/templates/preferences/explain.html` | **The two-panel AI page** | Changing how AI output is presented |
| `app/templates/errors/*.html` | 403 / 404 / 500 / generic pages | Changing error pages |
| `app/static/css/app.css` | Project-specific CSS, including the two panel colours | Small style changes |
| `app/static/js/loading.js` | Navigation loading indicator | Changing loading feedback |
| `app/static/vendor/` | Bootstrap | **Do not edit.** Replace the file to upgrade |
| **`migrations/versions/`** | The schema history | **Add** via `flask db migrate`. **Never delete** |
| `scripts/seed_demo.py` | Demo accounts, categories, 18 menu items | Changing demo data |
| `scripts/build_dataset.py` | Dataset splitting. **Defaults to v2** | 🔴 Frozen |
| `scripts/smoke_local_model.py` | Standalone model load check | Rarely |
| `training/train_lora.py` | LoRA training. Defaults to v4 | 🔴 Frozen — do not run |
| `training/evaluate*.py` | Evaluation harnesses | 🔴 Frozen |
| `data/raw/*.jsonl` | The hand-authored datasets | 🔴 Frozen at v4 |
| `models/` | Weights and adapters | 🔴 Never modify. Back up |
| `tests/test_preference_safeguard.py` | 60 tests pinning the safeguard | Add only. **Never weaken** |
| `.env` | Real secrets | Local only. **Never commit** |
| `.env.example` | The committed template | When adding a config variable |
| `requirements.txt` | Web dependencies | Adding a dependency — pin it |
| `requirements-ai.txt` | AI dependencies | See §8.4 of the MySQL guide first |

---

## 25 — "If you need to change X, go here"

### Change how the menu looks

`app/templates/menu/index.html` (grid), `app/templates/menu/detail.html` (one dish),
`app/templates/partials/_menu_card.html` (the card — **used by three pages**, so a
change here propagates to the home page and the recommendations page too).
Styling: `app/static/css/app.css`, or Bootstrap classes in the templates.

### Change the menu data

For demo data: `scripts/seed_demo.py`, then re-run it (it is idempotent — existing rows
are not modified, so change an *existing* dish through the admin UI instead).
For live data: the admin UI at `/admin/menu`, which goes through
`app/services/menu.py`.

### Change how recommendations are calculated

`app/services/recommendations.py`.

| Want to change | Edit |
| --- | --- |
| What text is compared | `build_item_document()` / `build_preference_document()` |
| The dietary rules | `_DIETARY_COMPATIBILITY` — **safety-critical** |
| How much history counts | `HISTORY_WEIGHT` (currently 0.5) |
| How many results | `MAX_RECOMMENDATIONS` (currently 10) |
| The match labels | `Recommendation.match_label` thresholds |

Then update `tests/test_recommendations.py`.

### Change the AI wording

Two different places, depending on what you mean:

* **The prompt** → `build_prompt()` in `app/services/local_llm.py`.
  ⚠️ The prompt shape matches the format the V4 dataset was authored in. Changing it
  degrades output, because the adapter was tuned on that exact shape.
* **The fallback wording** → `deterministic_explanation()` and the prose tables
  `_SPICE_MATCHED`, `_SPICE_BARE`, `_cuisine_word()`.
* **Explanation length** → `LLM_MAX_NEW_TOKENS`, or `_tidy()`'s two-sentence cut.

### Change the AI safety behaviour

`app/services/local_llm.py`, the section headed
`# Preference-safety guard (M07.7)`.

**Read §16 in full first.** Then:

| Want to change | Edit |
| --- | --- |
| Detected phrasings | `_CLAIM_VERB`, `_MATCH_VERB`, `_DIMENSION_NOUNS`, `_VALUE_WORDS` |
| Placeholder detection | `_PLACEHOLDER`, `_PLACEHOLDER_AS_VALUE`, `_CUSTOMER_STATEMENT` |
| The replacement text | `deterministic_explanation()` |

🔴 **Do not remove the safeguard, and do not add a switch that disables it** (§16.11).
Every change here needs a test in `tests/test_preference_safeguard.py`.

### Change the model

`LLM_ADAPTER_PATH` in `.env` selects the adapter, and `LLM_MODEL_PATH` the base.

🔴 **v4 is production and training is frozen** (§19). Changing the adapter changes the
product's behaviour and invalidates the 25/25 evaluation result. If a future
requirement genuinely demands it: read `docs/AI.md` §§15–18, keep v4 untouched, write
to a **new** directory, and evaluate against the same 25 held-out cases first.

### Change authentication

| Want to change | Edit |
| --- | --- |
| Registration or login flow | `app/routes/auth.py` |
| Credential verification | `app/services/auth.py` |
| Password policy | `app/utils/security.py` — `PASSWORD_MIN_LENGTH`, `password_policy_errors()`, `_COMMON_PASSWORDS` |
| Session behaviour, decorators | `app/utils/authorization.py` |
| Cookie settings, session lifetime | `config.py` — `SESSION_COOKIE_*`, `PERMANENT_SESSION_LIFETIME` |
| Rate limits | `app/utils/ratelimit.py` |
| The forms | `app/routes/forms.py` |

Then update `tests/test_auth.py` and `tests/test_session_revocation.py`.

### Change the database schema

**Always in this order:**

1. Edit the model in `app/models/`.
2. Make sure it is imported in `app/models/__init__.py` — *a model that is never
   imported is silently missing from every autogenerated migration*.
3. Generate the migration:
   ```powershell
   flask --app run.py db migrate -m "describe the change"
   ```
4. **Read the generated file.** Autogenerate misses `CHECK` constraint renames, enum
   value changes and server defaults. Fix it by hand where needed.
5. Apply and verify:
   ```powershell
   flask --app run.py db upgrade
   flask --app run.py db current
   ```
6. Run `pytest -q` — `tests/test_migrations.py` exercises the chain.

🔴 Never delete an existing migration. 🔴 Never edit one that has already been applied
elsewhere.

### Change prices or the currency

| Want to change | Edit |
| --- | --- |
| A dish's price | The admin UI, or `scripts/seed_demo.py` for demo data |
| The currency symbol | `CURRENCY_SYMBOL` in `app/utils/formatting.py` — **one place** |
| Price formatting | `money()` in the same file |
| The maximum price | `MAX_PRICE` in `app/services/menu.py` (currently 100000.00) |
| Adding tax or a delivery fee | `checkout()` in `app/services/orders.py`. `Order.total` already exists as a separate column specifically so it can diverge from `subtotal` without a schema change |

### Change navigation

`app/templates/base.html`. Role visibility is the `{% if current_user.role.value ... %}`
blocks — and remember that hiding a link is **not** a security control (§6.4).

### Change error pages

`app/templates/errors/` for the markup; `app/utils/errors.py` for which template maps
to which status (`_ERROR_TEMPLATES`) and for the HTML/JSON negotiation.

### Change the order status workflow

`ALLOWED_STATUS_TRANSITIONS` in `app/services/orders.py`. That one dict drives both the
server-side check and the buttons the staff UI renders. Update
`tests/test_staff_orders.py`.

### Add a new audited event

1. Add the value to `AuditEvent` in `app/models/audit_log.py`.
2. **Generate a migration** — the column is `VARCHAR` + `CHECK`, so the allow-list is
   enforced in the database. Migration `38297b707b89` is the worked example of exactly
   this.
3. Call `record_event(...)` from the route.

### Add a new page

1. Route in the appropriate `app/routes/*.py` (or a new blueprint registered in
   `app/routes/__init__.py`).
2. Business logic in `app/services/`, **not** in the route.
3. Template extending `base.html`.
4. Navbar link in `base.html` if it needs one.
5. Tests.

---

## 26 — Complete request lifecycle

### Example: a customer gets an AI-explained recommendation

Every step below was verified against the source.

```text
──────────────────────────────────────────────────────────────────────────────
PHASE 1 — SAVING PREFERENCES
──────────────────────────────────────────────────────────────────────────────

 1. Browser: POST /preferences
       csrf_token, dietary_preference=vegetarian,
       cuisine_preference=indian, spice_preference=hot

 2. Flask matches the rule → app/routes/preferences.py::preferences

 3. @login_required → get_current_user()
       session["user_id"] → SELECT users WHERE id = ?
       is_active?  session["sv"] == user.session_version?
       → yes, request proceeds

 4. PreferenceForm().validate_on_submit()
       CSRF token verified by Flask-WTF
       each SelectField value checked against its choices

 5. app/services/preferences.py::update_preferences(user.id, PreferenceInput(...))
       _coerce(): "" → None; otherwise must be a real enum member
       row located BY user_id  (never by a client-supplied row id)
       INSERT or UPDATE customer_preferences
       db.session.commit()

 6. flash("Preferences saved.")  →  redirect 302 → /recommendations

──────────────────────────────────────────────────────────────────────────────
PHASE 2 — RANKING
──────────────────────────────────────────────────────────────────────────────

 7. Browser: GET /recommendations
       → app/routes/preferences.py::recommendations
       → @login_required (as step 3)

 8. get_preferences(user.id)  →  CustomerPreference row

 9. app/services/recommendations.py::recommend_for_user(user.id, preference)

      9a. candidate_items(preference)
             SELECT menu_items JOIN categories
             WHERE menu_items.is_available IS true
               AND categories.is_active IS true
             ORDER BY menu_items.name
             with joinedload(category) + selectinload(ingredients)
             → then the DIETARY HARD FILTER removes anything
               _DIETARY_COMPATIBILITY[vegetarian] does not allow.
               vegetarian → {vegan, vegetarian}
               ⇒ every non-vegetarian dish is GONE, not merely ranked lower

      9b. build_preference_document(preference)
             → "indian spice_hot diet_vegetarian"

      9c. build_history_document(user_id, allowed)
             SELECT menu_items JOIN order_items JOIN orders
             WHERE orders.user_id = ? AND orders.status = 'completed'
             → filtered again by the allowed dietary set
             (empty for a new account)

      9d. build_item_document(item) for every candidate
             name + description + category + cuisine
             + "spice_hot" + "diet_vegetarian" + ingredients
             lowercased. NO price. NO id.

      9e. TfidfVectorizer(token_pattern=r"[a-z0-9_]+").fit_transform(documents)
          query_vector = transform([preference_document])
                       ( + history_vector * 0.5 if history exists )
          scores = cosine_similarity(query_vector, item_matrix)[0]

      9f. sorted by (-score, menu_item.name)   ← stable tie-break
          return the top 10

10. render_template("preferences/recommendations.html", ...)
       each card: name, ₹price (money filter), badges,
       match_label, score_percent, progress bar

11. HTML → browser.   Measured: 16–62 ms.

──────────────────────────────────────────────────────────────────────────────
PHASE 3 — THE AI EXPLANATION
──────────────────────────────────────────────────────────────────────────────

12. Browser: GET /recommendations/explain     (no parameters at all)
       → app/routes/preferences.py::explain
       → @login_required

13. get_preferences(user.id)  and  recommend_for_user(...)  run AGAIN
       (deterministic, so the answer is identical)
       top = results[0]

14. ExplanationRequest(
        item_name      = top.menu_item.name,
        item_cuisine   = top.menu_item.cuisine.value,
        item_dietary   = top.menu_item.dietary_type.value,
        item_spice     = top.menu_item.spice_level.value,
        preferred_cuisine = preference.cuisine_preference.value or None,
        preferred_dietary = preference.dietary_preference.value or None,
        preferred_spice   = preference.spice_preference.value   or None,
        match_label    = top.match_label,
    )
    NO price. NO id. NO username. NO order history.

15. generate_explanation(request)

     15a. is_enabled()?  LLM_ENABLED → yes

     15b. _load()
             already warmed up (§14) → returns the cached
             (tokenizer, PeftModelForCausalLM) instantly

     15c. build_prompt(request)
             every free-text value through _sanitise() first

             Explain in one or two sentences why this menu item was
             recommended. Use only the facts provided.

             Customer preference:
             cuisine: indian
             dietary: vegetarian
             spice: hot

             Recommended item:
             name: Paneer Tikka
             cuisine: indian
             dietary: vegetarian
             spice: hot
             match: Good match

             Explanation:

     15d. tokenizer(prompt, return_tensors="pt")

     15e. with torch.no_grad():
              model.generate(max_new_tokens=48,
                             do_sample=False,          ← greedy
                             pad_token_id=eos)
          ~6 seconds on CPU

     15f. decode, skipping the prompt tokens

     15g. _tidy(text)   first 1–2 sentences, ≤400 chars   (cosmetic)

     15h. apply_preference_safeguard(text, request)      ← UNCONDITIONAL
             explanation_is_preference_safe(text, request)?
               _leaks_placeholder()?
               all preferences None → _claims_any_preference()?
               per unset dimension → _claims_unset_preference()?

             SAFE   → return the model's own sentence
             UNSAFE → log "Explanation rejected: it credited a preference
                          the customer did not set"
                      return deterministic_explanation(request)

16. render_template("preferences/explain.html", top, preference, explanation)

       LEFT PANEL  .qj-facts-panel   green border, "Calculated by Quick Junction"
          Price ₹249.00
          Cuisine       Indian      matches your preference
          Dietary type  Vegetarian  allowed by your diet
          Spice level   Hot         matches your preference
          Match         Good match  35%

       RIGHT PANEL .qj-ai-panel     blue border, "Advisory"
          "Paneer Tikka is Indian, vegetarian and hot,
           matching all three preferences you set."
          — or, if explanation is None —
          "AI explanation temporarily unavailable. The recommendation
           itself is unaffected — it is calculated without the model."

17. HTML → browser.   Measured: 6.1–6.3 s warm.
```

**The three things to take from this trace:**

1. **The model appears at step 15e** — after the item has already been chosen,
   filtered, ranked and validated.
2. **Step 15h is not optional.** It runs on every generation, and it is the last thing
   that touches the text before it is returned.
3. **Step 16 shows both panels, always.** The authoritative facts are never replaced by
   the prose; they sit beside it, labelled.

---

## 27 — Complete startup lifecycle

```text
python run.py
      │
      ├─► import run
      │      │
      │      ├─► from app import create_app
      │      │      │
      │      │      └─► import app.services... (transitively)
      │      │             app/services/local_llm.py sets, at module level:
      │      │                HF_HUB_OFFLINE=1  TRANSFORMERS_OFFLINE=1
      │      │                USE_TF=0  USE_FLAX=0  USE_JAX=0
      │      │             ← BEFORE transformers is imported anywhere
      │      │
      │      └─► app = create_app()
      │
      ├─► 1. get_config(None) → APP_ENV → DevelopmentConfig
      │        (config.py already ran load_dotenv(BASE_DIR/".env") on import)
      │
      ├─► 2. app = Flask(__name__, instance_relative_config=True)
      │      app.config.from_object(config_class)
      │
      ├─► 3. config_class.validate()
      │        SECRET_KEY present?  DATABASE_URL present?
      │        production also: debug off? key strong? not SQLite?
      │        ✗ → ConfigError, process exits.  Nothing else has been built yet.
      │
      ├─► 4. configure_logging(app)
      │        stream handler + rotating logs/app.log + logs/security.log
      │        ► "Logging configured for development environment"
      │
      ├─► 5. db.init_app(app)         Flask-SQLAlchemy
      │      migrate.init_app(app, db)  Flask-Migrate / Alembic
      │      csrf.init_app(app)       CSRFProtect, application-wide
      │
      ├─► 6. from app import models
      │        registers all 9 model modules on db.metadata
      │        (a model never imported is invisible to `flask db migrate`)
      │
      ├─► 7. register_error_handlers(app)
      │        HTTPException handler + catch-all Exception handler
      │
      ├─► 8. register_filters(app)
      │        jinja_env.filters["money"]
      │
      ├─► 9. register_blueprints(app)
      │        health, main, auth, account, menu, admin_menu,
      │        cart, orders, staff_orders, preferences
      │
      ├─► 10. app.context_processor(_template_globals)
      │        injects current_user and cart_count into every template
      │
      ├─► 11. warm_up(app)
      │         guards: not already started, not TESTING,
      │                 LLM_ENABLED, LLM_WARMUP,
      │                 and (not DEBUG or WERKZEUG_RUN_MAIN == "true")
      │         ► "Local LLM warm-up started in the background"
      │         daemon thread → _load():
      │              AutoTokenizer.from_pretrained(local_files_only=True)
      │              AutoModelForCausalLM.from_pretrained(float32, local_files_only=True)
      │              PeftModel.from_pretrained(adapter, local_files_only=True)
      │              ► "Loaded LoRA adapter from ...qwen3-0.6b-quickjunction-lora-v4"
      │              model.eval()
      │         ► "Local LLM warmed up in 15.6s; first explanation will be fast"
      │         RETURNS IMMEDIATELY — startup is never blocked
      │
      └─► 12. app.run(host=127.0.0.1, port=5000, debug=True)
                ► " * Serving Flask app 'app'"
                ► " * Running on http://127.0.0.1:5000"

              Process start → serving: ~2 s
              Warm-up completes in the background at ~15.6 s
```

**No database connection is made during startup.** The engine is lazy — the first query
of the first request opens it. That is why the application starts successfully even
when MySQL is down, and only fails when a page tries to read data (§21.6).

---

## 28 — Handover rules

### ✅ DO

* **Read `docs/PROJECT_PROGRESS.md` before major work.** It is the authoritative record
  of what has actually been implemented. Its own header states the rule: *"If a feature
  is not listed here as complete, assume it does not exist."*
* **Run `pytest -q` before you change anything**, so you know the baseline is green
  before you touch it.
* **Run it again before you commit.**
* **Keep every migration.** Add new ones; never delete or rewrite applied ones.
* **Keep `.env` private.** It is git-ignored — keep it that way.
* **Back up `models/`.** 1.4 GB, not in Git, no documented download source. The V4
  adapter is a product of this project and would cost a retrain to reproduce.
* **Back up the database** before any schema work (`docs/MYSQL_SETUP_HANDOFF.md` §16).
* **Verify AI behaviour after any change to `local_llm.py`**, through the real
  application — not only through the unit tests. `docs/AI.md` §18.4 records that the
  live application found a defect the 25-case harness never produced.
* **Confirm the adapter actually loaded** on every fresh environment — look for
  `Loaded LoRA adapter from ...v4` in the log (§13.7).
* **Preserve the safeguard.** It is why V4 is in production.
* **Put business logic in services**, not in routes.
* **Pin every new dependency** to an exact version.
* **Update the documentation after every milestone**, including this file.

### 🔴 DO NOT

* **Do not train models.** Training is complete and frozen. `HANDOFF.md`: *"no further
  training is planned or required."*
* **Do not modify `models/`, `data/`, or `training/`.**
* **Do not delete `migrations/`** to fix a database problem. Reset the *database*
  instead (`docs/MYSQL_SETUP_HANDOFF.md` §9.7).
* **Do not commit `.env`.** If you ever do: rotate the `SECRET_KEY` and the database
  password **first**, clean history second.
* **Do not commit model weights** unless you are deliberately changing the distribution
  strategy — and if you do, document why.
* **Do not bypass, disable, or weaken the preference safeguard**, and do not add a
  configuration switch that turns it off (§16.11).
* **Do not change `LLM_ADAPTER_PATH` casually.** v4 is production and the 25/25 result
  is specific to it.
* **Do not use MySQL `root` as the application's database account** (§5 of the MySQL
  guide).
* **Do not weaken tests to make a suite green.** Fix the code, or change the test
  deliberately and say so in the commit message.
* **Do not put business logic in routes** or `flask.request` access in services.
* **Do not use `|safe` in a template** — especially not on model output.
* **Do not trust anything from the browser** for a price, a total, an identity, or a
  role.
* **Do not add a CDN dependency.** The project is offline-first, and Bootstrap is
  vendored for exactly that reason.

---

## 29 — Final handover checklist

```text
REPOSITORY
[ ] Repository cloned or copied
[ ] git status is clean
[ ] Confirm HEAD is the expected commit

PYTHON
[ ] Python 3.10+ installed and on PATH
[ ] Virtual environment created:  python -m venv .venv
[ ] Activated:  .\.venv\Scripts\Activate.ps1
[ ] (Get-Command python).Source points inside .venv
[ ] pip install -r requirements.txt
[ ] pip install -r requirements-ai.txt          (only if AI is required)
[ ] pip check  →  No broken requirements found.

DATABASE
[ ] MySQL 8.0+ installed and the service is Running
[ ] Database quick_junction created (utf8mb4 / utf8mb4_unicode_ci)
[ ] Application user qj_user created for BOTH @'127.0.0.1' and @'localhost'
[ ] Privileges granted on quick_junction.* only  (not on *.*)
[ ] Verified:  mysql -u qj_user -p -h 127.0.0.1 quick_junction  connects

CONFIGURATION
[ ] .env created from .env.example
[ ] SECRET_KEY generated (secrets.token_urlsafe(64)), not the placeholder
[ ] DATABASE_URL set, password URL-encoded if it contains special characters
[ ] LLM_* left commented out (built-in defaults are correct)
[ ] .env is NOT tracked by Git:  git ls-files | Select-String "^\.env$"  → empty

SCHEMA
[ ] flask --app run.py db upgrade  completed
[ ] flask --app run.py db current  →  7c4e1a9b52d3 (head)
[ ] flask --app run.py db heads    →  exactly one line
[ ] SHOW TABLES;  →  10 tables

DEMO DATA
[ ] python scripts/seed_demo.py
[ ] 3 categories, 18 menu items, 3 users
[ ] Understood that demo-password-1 is PUBLIC and demo-only

MODEL  — SKIP THIS BLOCK IF AI IS NOT REQUIRED
[ ] models/ transferred from the project owner (~1.4 GB)
[ ] models/Qwen3-0.6B-Base/ present
[ ] models/qwen3-0.6b-quickjunction-lora-v4/ present
[ ] Optional:  python scripts/smoke_local_model.py
[ ] ⚠ V4 LOADED — startup log shows:
      "Loaded LoRA adapter from ...qwen3-0.6b-quickjunction-lora-v4"
      NOT "Could not load LoRA adapter; continuing with base model"

STARTUP
[ ] python run.py
[ ] "Logging configured for development environment"
[ ] "Running on http://127.0.0.1:5000"
[ ] With AI: "Local LLM warmed up in ...s"

FUNCTIONAL VERIFICATION
[ ] Landing page /            loads
[ ] Menu /menu               17 of 18 items (Sold Out Biryani hidden)
[ ] Invalid URL              styled 404, not a traceback
[ ] Logged-out /orders       redirects to /login with a flash message
[ ] Login as customer        succeeds
[ ] Wrong password AND unknown user give the IDENTICAL message
[ ] Preferences              Indian / Vegetarian / Hot → saved
[ ] Recommendations          Paneer Tikka ~35% "Good match" at the top
[ ] ⚠ NO non-vegetarian dish appears           ← the dietary hard filter
[ ] AI explanation           a grounded sentence, or the graceful notice
[ ] Cart                     add an item; navbar badge increments
[ ] Checkout                 order placed; totals correct
[ ] Order history            the order appears as "pending"
[ ] Staff flow               log in as staff; /staff/orders shows the queue;
                             only legal transitions are offered
[ ] Admin flow               log in as admin; /admin/menu shows all 18 items
[ ] Authorization            as customer, /admin/menu returns 403

TESTS
[ ] pytest -q  →  420 passed, 0 failures
[ ] No test was weakened or removed

SECURITY & BACKUP
[ ] .env not committed; SECRET_KEY is unique to this environment
[ ] Demo passwords changed or demo accounts removed for any shared machine
[ ] Database backup taken and a restore rehearsed
[ ] models/ backed up
[ ] Understood docs/SECURITY.md §17 — what is NOT yet implemented

DOCUMENTATION READ
[ ] docs/MYSQL_SETUP_HANDOFF.md    installation
[ ] docs/TECHNICAL_HANDOFF.md      this document
[ ] docs/PROJECT_PROGRESS.md       what actually exists
[ ] docs/AI.md §18                 the safeguard and why V4 was promoted
[ ] docs/SECURITY.md §17           the known gaps
```

---

## Document map

| Document | Read it for |
| --- | --- |
| **`docs/MYSQL_SETUP_HANDOFF.md`** | Installing from nothing on a new Windows machine |
| **`docs/TECHNICAL_HANDOFF.md`** | This file — how the system works |
| `README.md` | Project overview and rationale |
| `HANDOFF.md` | The condensed one-page setup summary |
| `docs/PROJECT_PROGRESS.md` | **The authoritative record** of what has been built |
| `docs/ARCHITECTURE.md` | Layer-by-layer design notes |
| `docs/DATABASE.md` | Deeper schema reasoning |
| `docs/SECURITY.md` | The full security model, including §17's outstanding list |
| `docs/AI.md` | The complete AI record, milestone by milestone |
| `data/README.md` | Dataset documentation |

---

*Verified against commit `912150a` on 2026-08-20. Production adapter:
`models/qwen3-0.6b-quickjunction-lora-v4`. Dataset: v4, 240 hand-authored examples.
Migration head: `2d9f3b20045f`. Database: MySQL 8.0.46. Test suite: 420 passed,
0 failed, 0 skipped.*

*Phase 3 (2026-09-27): migration head is now `7c4e1a9b52d3` (staff approval); see
`docs/PHASE3_PRODUCT_IMPROVEMENTS.md`.*
