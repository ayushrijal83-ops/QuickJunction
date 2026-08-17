# Quick Junction — Project Progress

**Authoritative project-progress document.** Records only work that has
actually been implemented and verified against the repository. If a
feature is not listed here as complete, assume it does not exist.

Last updated: 2026-08-17 (end of Milestone 05)
Current state: **Milestone 05 complete.** Next: Milestone 06.

> **Document history:** this file did not exist until the end of Milestone
> 04. Milestones 01–03 below were reconstructed by reading the repository
> and `docs/ARCHITECTURE.md` / `DATABASE.md` / `SECURITY.md`, not from a
> prior progress record — so their "tests run" and "verified" entries
> describe the state observable today, not a claim about what was executed
> at the time each milestone was originally built.

---

## Status at a glance

| Milestone | Scope | Status |
| --- | --- | --- |
| 01 | Foundation: app factory, config, logging, `/health` | ✅ Complete |
| 02 | Auth, roles, CSRF, sessions, rate limiting, audit trail | ✅ Complete |
| 03 | Menu data layer + menu management | ✅ Complete |
| 04 | Cart, checkout, order placement, order history | ✅ Complete |
| 05 | Staff order management + status workflow | ✅ Complete |
| 06 | Not yet defined | ⬜ Not started |
| — | Payment gateway, recommender, AI assistant, deployment | ⬜ Not started |

**Test suite: 117 passing, 0 failing** (`pytest`; in-memory SQLite for
application tests, temporary on-disk SQLite for migration tests).
**MySQL verification: PENDING** — see [MySQL verification status](#mysql-verification-status).

---

## Milestone 01 — Foundation ✅

Application factory (`create_app`), three configs
(development/testing/production) with fail-fast `validate()`, two-channel
logging (`logs/app.log`, `logs/security.log`), `GET /health` returning
exactly `{"status": "ok"}`.

**Verified:** 10 tests in `tests/test_foundation.py` — factory builds,
unknown config rejected, testing config isolated, health endpoint exact
response, database configured, secrets never in responses, production
refuses debug/weak secret/SQLite.

---

## Milestone 02 — Auth, authorization, CSRF, audit ✅

- `User` (Argon2id via `argon2-cffi`, roles ADMIN/STAFF/CUSTOMER as a
  CHECK-constrained VARCHAR), `AuditLog` + `AuditEvent`.
- `/register`, `/login`, `/logout`; `/account/*` authorization-foundation
  endpoints.
- Session-based auth holding only a user id; user re-loaded from the
  database every request. `login_user()` clears the session first
  (fixation protection).
- `CSRFProtect` app-wide; in-memory login rate limiter (5 failures per
  IP+username per 15 min).
- `record_event()` — the only `audit_logs` writer; refuses sensitive
  metadata keys.

**Verified:** 29 tests in `tests/test_auth.py`.
**Migration:** `abf997064564_add_users_and_audit_logs_tables.py`.

---

## Milestone 03 — Menu data layer + menu management ✅

- `Category`, `MenuItem`, `Ingredient`, `MenuItemIngredient`,
  `CustomerPreference`. Price is `Numeric(10,2)`→`Decimal` with
  `CHECK (price > 0)`.
- Allow-listed enums (`cuisine`, `spice_level`, `dietary_type`) backed by
  real database CHECK constraints via `enum_column()`.
- Public menu (`/menu`, `/menu/<id>`) — active category + available item
  only. Admin/staff management (`/admin/categories/*`, `/admin/menu/*`) —
  STAFF may view, only ADMIN may write.
- 7 menu-management audit events.

**Verified:** 26 tests in `tests/test_menu.py`, including a raw SQL INSERT
proving the database itself rejects invalid enum values, SQL-injection
input stored inertly, and XSS payloads escaped by Jinja2.
**Migration:** `8d5ba0171efe_add_menu_data_layer_and_fix_missing_.py` —
also retrofitted two CHECK constraints that M02 had silently created as
bare VARCHARs (`create_constraint=True` was missing).

---

## Milestone 04 — Cart, checkout, order placement, order history ✅

### Implemented

**Models** (`app/models/order.py`, migration
`d8f3bfd0e2a9_add_cart_order_tables.py`):

- `Order` — `id`, `user_id` (FK→`users`, RESTRICT), `status`, `subtotal`,
  `total`, `created_at`, `updated_at`. CHECKs: `subtotal >= 0`,
  `total >= 0`. Index `ix_orders_user_created(user_id, created_at)`.
- `OrderItem` — `id`, `order_id` (FK→`orders`, CASCADE), `menu_item_id`
  (FK→`menu_items`, RESTRICT), `item_name_snapshot`,
  `unit_price_snapshot`, `quantity`, `line_total`. CHECKs:
  `quantity > 0`, `unit_price_snapshot >= 0`, `line_total >= 0`.
- `OrderStatus` — `pending`, `confirmed`, `preparing`, `ready`,
  `completed`, `cancelled`. **Only `pending` is ever written this
  milestone**; the full lifecycle exists so M05 is a new endpoint, not a
  schema change.
- All money is `Numeric(10,2)` → Python `Decimal`. No float anywhere.

**Cart** — session-only, no database table. `app/utils/cart.py` stores
`{menu_item_id: quantity}` in Flask's signed session cookie; no price,
name, or total is ever stored there. `app/services/cart.py` re-resolves
every line against the live `MenuItem` table on every read, enforcing
bounds (1–20 per item, ≤30 distinct items) and availability.

**Routes:**

| Route | Auth | Notes |
| --- | --- | --- |
| `GET /cart` | anonymous OK | live prices, re-resolved per request |
| `POST /cart/add\|update\|remove\|clear` | anonymous OK | CSRF-protected |
| `GET/POST /checkout` | login required | CSRF-protected |
| `GET /orders` | login required | own orders only |
| `GET /orders/<id>` | login required | ownership in query; 404 on mismatch |

**Checkout** (`app/services/orders.py::checkout`) — one transaction: parse
cart → reload each menu item → verify availability → verify quantities →
recompute every unit price and line total → sum subtotal/total → write
`Order` + `OrderItem` → commit. Any failure raises `CheckoutError` with a
full rollback; no partial order is possible. Cart cleared only after
success.

**Audit:** `order_created` (with `order_id` + `total` in metadata) and
`order_creation_failed` added to `AuditEvent` (now 14 events total).

### Tests run — 28 new, all passing

`tests/test_orders.py`, covering all 28 required scenarios: empty cart;
add valid item; unavailable item rejected; invalid menu id rejected;
zero/negative/excessive quantity rejected; cart update/remove/clear;
checkout requires auth; successful checkout; server-calculated
subtotal/total; client price/subtotal/total manipulation ignored; price
snapshot stored and immune to later menu edits; transaction rollback on
failure; history shows only own orders; IDOR rejected; customer cannot
change status; CSRF enforced; order-created audit event; unavailable item
rejected at checkout; quantity boundaries; Decimal precision; cart cleared
after checkout.

**Full suite: 93 passing** (foundation 10, auth 29, menu 26, orders 28).

### Security checks actually performed

Automated (in `tests/test_orders.py`) **and** re-verified by hand against
the running dev server in Chrome:

- **Price/subtotal/total manipulation** — injected hidden `total`,
  `subtotal`, `unit_price`, and `status` fields into the real checkout
  form via the DevTools console and submitted. Order was created with the
  correct server price (349.00) and status `pending`; every injected value
  ignored. There is no form field or code path that reads a price.
- **IDOR** — logged in as a second account and requested the first
  account's order id directly. Got a plain **404**, not 403 (403 would
  itself confirm the id exists). Second account's own history correctly
  showed zero orders.
- **Authentication** — logged out, then `GET /orders` and
  `GET /orders/<id>` both returned 401.
- **CSRF** — raw `fetch()` POST to `/cart/add` with no token returned 400
  against the live server.
- **Unavailable items** — deactivated item never appears on the public
  menu; deactivating an item already in a cart blocks checkout and logs
  `order_creation_failed`.
- **Audit log inspection** — read `logs/security.log` after the
  walkthrough; `order_created` present, no password or session data.

Reviewed and found clean: client-trusted prices (none), float money
(none), missing authorization (none), missing CSRF (none), unsafe SQL
(ORM/bound parameters only), transaction handling, sensitive data in
logs, debug leakage.

### Migration results

`flask db upgrade` empty → all 3 revisions → `downgrade` → re-`upgrade`,
verified clean twice against a scratch SQLite database. Emitted DDL
inspected directly (CHECK constraints, FK actions, indexes all correct).

**One real bug found and fixed during this milestone.** The M04 migration
initially stripped the `ck_audit_logs_event_type` diff as an Alembic false
positive along with three genuinely-unrelated constraint diffs — but that
one was **real**, because `AuditEvent` gained two members. It was caught by
a live checkout in browser testing raising `IntegrityError`, **not** by the
test suite: tests build the schema with `create_all()` from current models
and therefore bypass migrations entirely. Fixed by hand-widening the
constraint in both `upgrade()` and `downgrade()`.

---

## Milestone 05 — Staff order management + status workflow ✅

### Implemented

**Routes** (`app/routes/staff_orders.py`), all
`@require_role(Role.STAFF, Role.ADMIN)`:

| Route | Purpose |
| --- | --- |
| `GET /staff/orders` | order queue: id, customer username, placed time, status, item count, total |
| `GET /staff/orders/<id>` | detail: items, quantities, unit prices, line totals, subtotal, total, status, created time — all from `OrderItem` snapshots |
| `POST /staff/orders/<id>/status` | transition, CSRF-protected and workflow-validated |

ADMIN reuses these routes and services rather than getting a separate
interface.

**Status workflow** (`ALLOWED_STATUS_TRANSITIONS` in
`app/services/orders.py`) — an explicit server-side allow-list:

```
PENDING   -> CONFIRMED | CANCELLED
CONFIRMED -> PREPARING | CANCELLED
PREPARING -> READY     | CANCELLED
READY     -> COMPLETED
COMPLETED -> (terminal)   CANCELLED -> (terminal)
```

Everything else is rejected: backwards moves, skips, same-status no-ops,
and any value outside the enum. `allowed_next_statuses()` drives which
buttons render, but the service re-validates against the order's real
current status on every POST, so the UI is never the control.

Cancellation is intentionally minimal — it sets the status and nothing
more. No refund, restock, notification, or customer-initiated cancellation
was built.

**Service functions added:** `is_valid_transition`, `allowed_next_statuses`,
`list_orders_for_staff` (eager-loads items + customer to avoid N+1),
`get_order_for_staff`, `update_order_status`, `coerce_status`.

**Model:** added a read-only `Order.customer` relationship for the queue.
No column, table, or constraint on `orders`/`order_items` changed.

**Audit:** `order_status_changed` and `order_status_change_rejected` added
to `AuditEvent` (now 16 events), with `user_id` = the acting staff/admin
member and `{order_id, from, to|attempted}` metadata.

**Templates:** `app/templates/staff/order_list.html`, `order_detail.html`.
The queue shows **username only** — no customer email or other PII.

### Migration status — one new revision

`migrations/versions/38297b707b89_widen_audit_event_allow_list_for_order_.py`
(`down_revision = d8f3bfd0e2a9`) widens `ck_audit_logs_event_type` for the
two new events. **No table changed** — the M04 decision to give
`orders.status` the full lifecycle up front meant M05 needed no schema
change to the order tables.

Written by hand via `flask db revision` (not `--autogenerate`), for the
reason recorded under M04: autogenerate reports this constraint as
"changed" on every run regardless, so it cannot distinguish a real widening
from a false positive.

Verified against scratch SQLite: empty → all four revisions → downgrade one
→ re-upgrade → downgrade to base → re-upgrade, all clean. The emitted
`ck_audit_logs_event_type` was inspected directly and contains all 16 event
values.

**Pre-existing chain also verified consistent with the models** (the
mandated M04 follow-up): a schema-diff harness confirmed the full migration
chain produces exactly the schema `db.create_all()` builds from the models
— columns, foreign keys, indexes and CHECK constraints across all 9 tables.
No inconsistency was found; the M04 fix had held.

### Testing gap from M04 — closed

`tests/test_migrations.py` (4 tests) now runs the **real Alembic chain**
against a temporary on-disk SQLite database:

1. the chain runs from empty and creates the expected tables;
2. the migrated schema matches the models' schema exactly (columns, FKs,
   indexes, CHECKs) — this is the check that would have caught the M04 bug;
3. every `AuditEvent` member satisfies the migrated
   `ck_audit_logs_event_type` — the M04 failure stated directly;
4. downgrade-to-base then re-upgrade reproduces the identical schema.

**This was proven non-vacuous:** temporarily adding an un-migrated enum
member made tests 2 and 3 fail with a clear message
(`AuditEvent members absent from the migrated CHECK: ['deliberate_test_canary']`);
the change was then reverted. The rest of the suite still uses
`create_all()` — deliberately, per the instruction not to redesign the test
infrastructure.

### Tests run — 24 new, all passing

- `tests/test_staff_orders.py` — **20 tests** covering all 16 required
  scenarios: staff route authentication (401); customer denied (403); staff
  allowed; admin allowed; order list fields; order detail snapshots;
  customer status visibility; customer cannot change status; valid
  transition chain PENDING→CONFIRMED→PREPARING→READY→COMPLETED; invalid
  transitions (backwards, skip, unknown value, from terminal); CSRF; forged
  role headers/fields; customer IDOR still intact; audit event on change;
  audit event on rejection; historical snapshots unchanged after a full
  status walk; unauthorized update rejected with no audit side effect; plus
  error-disclosure and 404 handling.
- `tests/test_migrations.py` — **4 tests** (above).

**Full suite: 117 passing, 0 failing** (foundation 10, auth 29, menu 26,
orders 28, migrations 4, staff orders 20). No regressions.

### Security checks actually performed

Automated in `tests/test_staff_orders.py` **and** re-verified by hand
against the running dev server in Chrome:

- **Unauthenticated** → `GET /staff/orders` returned 401.
- **Customer** → `/staff/orders`, `/staff/orders/<id>`, and the status POST
  all returned 403; order status unchanged afterwards.
- **Forged role** → `X-Role: staff` / `X-User-Role: admin` headers as a
  logged-in customer still returned 403.
- **Staff** → full workflow driven through the real UI:
  PENDING → CONFIRMED → PREPARING → READY → COMPLETED, each step confirmed
  on the page.
- **Invalid transition** → a forged `COMPLETED → PREPARING` POST *carrying a
  valid CSRF token* was rejected; the order stayed `completed` and an
  `order_status_change_rejected` row was written.
- **CSRF** → with a valid ADMIN session, a status POST with no token and
  one with a forged token both returned 400, order unchanged.
- **ADMIN** → accessed the queue and performed a real
  PENDING → CONFIRMED transition on another order.
- **Customer visibility** → after the staff-driven change, the customer's
  own `/orders` correctly showed order #1 as `completed`.
- **Price snapshots** → unit price 349.00 and total 698.00 were unchanged
  through all four transitions on screen; the automated test additionally
  repriced the live menu item to 999.00 first and confirmed no drift.
- **Audit inspection** → read `logs/security.log` and the `audit_logs`
  table: 4 `order_status_changed` rows plus 1
  `order_status_change_rejected`, each with the correct actor, order id and
  from/to values, and no credential or session material.
- **Static review of the new code** → no float arithmetic, no string-built
  SQL, no `|safe` in the new templates, no role read from
  `request.headers`/`form`/`args` anywhere in the app, and `require_role` on
  every staff route.

---

## MySQL verification status

**MySQL integration verification pending.**

No MySQL server has been reachable in this environment at any point. Every
migration and all DDL for M02, M03, M04, and M05 has been verified against
SQLite only — including the new `tests/test_migrations.py` suite, which
also runs on SQLite. This has **not** been tested against MySQL and must
not be described as if it had been.

Still to check against a real MySQL ≥ 8.0.16 instance:

- CHECK constraint creation and enforcement (native support requires
  ≥ 8.0.16; schema targets exactly that)
- `Numeric(10,2)` behaviour for `price`, `subtotal`, `total`,
  `unit_price_snapshot`, `line_total`
- Foreign key `ON DELETE RESTRICT` / `CASCADE` semantics under InnoDB
- Index creation, and per-schema uniqueness of CHECK constraint names

---

## Known issues

| # | Issue | Impact |
| --- | --- | --- |
| 1 | MySQL never verified (see above) | blocks any non-SQLite deployment |
| 2 | ~~Migrations not exercised by tests~~ — **closed in M05** by `tests/test_migrations.py`; note the application tests still use `create_all()` by design | — |
| 3 | Anonymous cart is discarded on login (`login_user()` clears the session for fixation protection) — no cart merge | user who builds a cart before signing in loses it |
| 4 | Checkout idempotency relies on the cart being cleared after success, not a dedicated idempotency key | concurrent double-submit under multiple workers |
| 5 | Rate limiter is in-process only (`dict` + lock) | ineffective across gunicorn workers/instances |
| 6 | Error handlers always return JSON, even for HTML routes | poor UX on an unexpected error in a browser |
| 7 | No security headers (HSTS, CSP, `X-Frame-Options`, …) | required before real users |
| 8 | Audit log retention not enforced in code | `audit_logs` holds IPs; needs a purge policy |
| 9 | `tests/__init__.py` exists solely to stop a stray `tests` package in site-packages from shadowing the local one | environment-specific workaround, not a project need |
| 10 | Order status changes have no concurrency guard — two staff moving the same order simultaneously both read the same current status, and the second write wins | rare double-transition under real concurrent kitchen use |
| 11 | `CANCELLED` sets a status and nothing else — no refund, restock, notification, or customer-initiated cancellation | cancellation is not operationally complete |
| 12 | The staff queue is unpaginated and unfiltered — every order, every load | degrades once order volume is non-trivial |
| 13 | `migrations/env.py` uses `db.get_engine()`, deprecated in Flask-SQLAlchemy 3.1 (12 warnings in the suite) — pre-existing, from the Flask-Migrate template | breaks on Flask-SQLAlchemy 3.2 |

Full ranked pre-production list: `docs/SECURITY.md` §17.

---

## Not implemented — do not assume these exist

Payment gateway / card processing · refunds · customer-initiated
cancellation · recommendation engine (TF-IDF / cosine similarity) · AI
assistant / LLM / fine-tuning · advanced analytics · reporting dashboard ·
deployment configuration · styled frontend (Bootstrap/JS — every page is
plain unstyled HTML) · password reset · email verification · staff queue
pagination/filtering.

`ai/`, `dataset/`, `training/` are intentionally empty placeholders.

---

## Exact current state (verified at end of M05)

| Item | Value |
| --- | --- |
| Tests | **117 passing, 0 failing**, 12 warnings (all the pre-existing `env.py` deprecation) |
| Test files | foundation 10, auth 29, menu 26, orders 28, migrations 4, staff orders 20 |
| Migrations | 4 revisions, head = `38297b707b89` |
| Tables | 9: `users`, `audit_logs`, `categories`, `menu_items`, `ingredients`, `menu_item_ingredients`, `customer_preferences`, `orders`, `order_items` |
| Models | 10 modules in `app/models/` |
| Blueprints | 8: health, auth, account, menu, admin_menu, cart, orders, staff_orders |
| Audit events | 16 |
| Order statuses | 6 (`pending`, `confirmed`, `preparing`, `ready`, `completed`, `cancelled`) |
| Roles | 3 (`admin`, `staff`, `customer`) |
| Database (tests) | in-memory SQLite; migration tests use temporary on-disk SQLite |
| Database (target) | MySQL 8 — **never verified**, see above |
| Runtime deps | pinned in `requirements.txt`; installed set matches |

---

## Next milestone

**Milestone 06 — not yet defined.** Scope is the Architect's call. With
roughly 7 days total for the MVP and M01–M05 complete, the ordering
lifecycle is functionally end-to-end (browse → cart → checkout → staff
fulfilment → customer sees status).

Candidates, in the order I would recommend them:

1. **Customer preferences UI + recommendation engine** — `CustomerPreference`
   has existed since M03 with no route to populate it, and it shares the
   exact `Cuisine`/`SpiceLevel`/`DietaryType` vocabulary with `MenuItem`
   specifically so this join needs no translation layer. This is the
   project's headline differentiator and the largest remaining gap.
2. **Frontend pass (Bootstrap)** — every page is currently unstyled HTML.
   Cheap, highly visible, and independent of the backend.
3. **MySQL verification + deployment prep** — known issue #1 is the only
   item that blocks a real deployment outright, and it needs a reachable
   MySQL server rather than more code.

The AI assistant (local Qwen3-0.6B-Base, `ai/` + `training/`) remains the
largest single unstarted piece; it should not be attempted until the
recommendation engine and a usable frontend exist.
