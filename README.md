# Quick Junction

A restaurant-management system: menu, orders, billing, and — in later
milestones — a locally hosted AI assistant and a menu recommendation engine.
No customer data ever leaves the deployment: the language model runs locally
and no external AI API is used.

## Current milestone

**Milestone 04 — Cart, checkout, order placement, order history.**

What exists:

- Everything from Milestones 01-03 (application factory, config,
  MySQL/SQLAlchemy, migrations, security baseline, logging, `GET /health`,
  registration/login/logout, roles, CSRF, rate limiting, audit trail, menu
  data layer, menu management)
- Session-based cart (`/cart`, `/cart/add|update|remove|clear`) — stores
  only `{menu_item_id: quantity}` in the signed session cookie, no price
- Secure checkout (`/checkout`) — every price, subtotal, and total is
  recomputed server-side from the live menu inside one database
  transaction; nothing submitted by the client is ever trusted
- `Order`/`OrderItem` models — price snapshots frozen at checkout time,
  money as `Decimal`/`Numeric`, `OrderStatus` carries the full lifecycle
  though only `PENDING` is written this milestone
- Order history (`/orders`, `/orders/<id>`) — scoped to the requesting
  user's own orders, IDOR-safe (ownership checked in the query itself)
- Order-placement events added to the audit trail
- pytest suite (93 tests: everything above + 28 for cart/checkout/order
  security — quantity/price/subtotal/total manipulation, IDOR, CSRF,
  transaction rollback, and audit coverage)

What deliberately does **not** exist yet: payment gateway, the
recommendation engine, the AI assistant, staff order-status management,
styled frontend (Bootstrap/JS) — every form so far is plain unstyled HTML,
functional only.

**MySQL verification is pending** — no MySQL server has been reachable in
this environment; every migration has been verified against SQLite only.
See `docs/DATABASE.md`.

## Technology stack

| Layer      | Choice                                       |
| ---------- | -------------------------------------------- |
| Language   | Python 3.10                                  |
| Web        | Flask 3 (application-factory pattern)        |
| ORM        | SQLAlchemy 2 via Flask-SQLAlchemy             |
| Migrations | Flask-Migrate / Alembic                      |
| Database   | MySQL (PyMySQL driver)                       |
| Tests      | pytest                                       |
| Frontend   | *later* — HTML, CSS, Bootstrap, JavaScript   |
| AI         | *later* — local Qwen3-0.6B-Base, LoRA/QLoRA  |
| Recommender| *later* — scikit-learn, TF-IDF + cosine      |

## Project layout

```
QuickJunction/
├── app/
│   ├── __init__.py        application factory
│   ├── extensions.py      db, migrate, csrf  (unbound instances)
│   ├── models/             User, AuditLog, Category, MenuItem, Ingredient,
│   │                       MenuItemIngredient, CustomerPreference,
│   │                       Order, OrderItem, enums.py
│   ├── routes/             blueprints: health, auth, account, menu,
│   │                       admin_menu, cart, orders (+ forms.py)
│   ├── templates/          register.html, login.html, account.html,
│   │                       menu/, admin/, cart/, orders/
│   ├── services/           auth.py, audit.py, menu.py, cart.py,
│   │                       orders.py — business logic
│   └── utils/              logging, errors, security, authorization,
│                           ratelimit, request_meta, cart
├── tests/                 pytest suite
├── migrations/            Alembic
├── ai/                    local model integration   (later)
├── dataset/                hand-built training data  (later)
├── training/               LoRA/QLoRA fine-tuning    (later)
├── models/                 local LLM weights — git-ignored
├── scripts/                one-off operational scripts
├── docs/                   ARCHITECTURE.md, SECURITY.md, DATABASE.md
├── config.py              development / testing / production
├── run.py                 development entry point
├── requirements.txt       web foundation
├── requirements-ai.txt    local AI stack (installed separately)
└── .env.example           template — no real values
```

## Local setup

Requires Python 3.10+ and a reachable MySQL 8 server.

```bash
# 1. Virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS

# 2. Dependencies
pip install -r requirements.txt

# 3. Environment
cp .env.example .env            # copy .env.example .env  on Windows
python -c "import secrets; print(secrets.token_urlsafe(64))"   # -> SECRET_KEY
```

Then edit `.env` and set `SECRET_KEY` and `DATABASE_URL`. **`.env` is
git-ignored and must never be committed.**

Create the database once:

```sql
CREATE DATABASE quick_junction CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'qj_user'@'localhost' IDENTIFIED BY '<strong-password>';
GRANT ALL PRIVILEGES ON quick_junction.* TO 'qj_user'@'localhost';
```

## How to run

```bash
python run.py                   # http://127.0.0.1:5000
curl http://127.0.0.1:5000/health
# {"status":"ok"}
```

Open `http://127.0.0.1:5000/register` in a browser to create an account,
then `/login`. A logged-in session lands on `/account/` (username, role,
logout button). `/account/me` returns the same identity as JSON.

`/menu` browses the public menu (no login needed). Admins can manage it at
`/admin/categories` and `/admin/menu` (create an ADMIN account by setting
its role directly in the database — there is no self-service admin
signup, by design).

Production is served by a WSGI server with `APP_ENV=production`; `run.py` is
for development only.

```bash
gunicorn "app:create_app('production')"
```

## Migrations

```bash
flask db migrate -m "describe the change"   # generate
flask db upgrade                            # apply
flask db downgrade                          # roll back one
```

Three revisions exist: `users`/`audit_logs` (M02); `categories`/
`menu_items`/`ingredients`/`menu_item_ingredients`/`customer_preferences`
plus two hand-written `CHECK` constraints retrofitted onto the M02 tables
(M03); `orders`/`order_items` plus a hand-written widening of
`ck_audit_logs_event_type` to cover the two new order audit events (M04 —
see `docs/DATABASE.md` for why these statements are hand-written rather
than autogenerated). Mechanics (upgrade from empty, downgrade, re-upgrade)
were verified against a scratch SQLite database for every revision —
no MySQL server was available in this environment. **The DDL has not been
verified against real MySQL**; do so before relying on it.

Always read a generated migration before applying it — Alembic autogenerate
guesses, and its guesses can drop columns.

## How to run tests

```bash
pytest
```

The suite uses an in-memory SQLite database, so no MySQL server is needed
and no real data is touched. Point `TEST_DATABASE_URL` at a throwaway MySQL
database when you need to verify MySQL-specific behaviour.

## Security rules

Full detail in [docs/SECURITY.md](docs/SECURITY.md). The non-negotiables:

1. **No secret is ever written into source.** Passwords, secret keys, API
   keys and DSNs come from the environment. `.env.example` holds
   placeholders only.
2. **Never commit `.env`.** It is in `.gitignore`; check `git status`
   before every commit anyway.
3. **Debug is never enabled outside development.** The production config
   refuses to start with `DEBUG` on, with a weak/placeholder `SECRET_KEY`,
   or with a non-MySQL database.
4. **Errors never leak internals.** Clients get a generic JSON error;
   stack traces go to the log.
5. **Validate every input at the trust boundary** — route handlers reject
   bad input before a service ever sees it.
6. **Never log a credential.** Log identifiers, not secrets; security
   events go to `logs/security.log` and the `audit_logs` table.
7. **Authorization is server-side only.** A role is read from the
   database on every request; nothing in a cookie, header, or form field
   is ever trusted to say who a user is or what they can do.
8. **Every state-changing form carries a CSRF token.** `GET` handlers stay
   side-effect free.
9. **Login errors never distinguish "no such user" from "wrong
   password."** Registration errors may name a duplicate field — that is
   normal signup UX, not the same risk (see `docs/SECURITY.md` §7).
10. **Money is `Decimal`, never `float`.** Every price is validated
    server-side and backed by a database `CHECK (price > 0)` — a request
    can never supply or override a price.
11. **Allow-listed fields are enforced at the database, not just the
    form.** `cuisine`, `spice_level`, `dietary_type` are backed by real
    `CHECK` constraints, not just a `<select>` (see `docs/DATABASE.md`).
12. **A client never supplies a price, subtotal, or total.** Checkout
    recomputes every value server-side from the live menu inside one
    transaction; the cart itself never stores a price (see
    `docs/SECURITY.md` §10).
13. **Row ownership is checked in the query, not after fetching.**
    `GET /orders/<id>` filters by the requesting user's id as part of the
    database query, so a mismatched id returns 404, never someone else's
    order (see `docs/SECURITY.md` §10).

If a secret is ever committed, treat it as compromised: rotate it first,
then clean the history.
