# Quick Junction — Project Progress

**Authoritative project-progress document.** Records only work that has
actually been implemented and verified against the repository. If a
feature is not listed here as complete, assume it does not exist.

Last updated: 2026-09-30 (end of Milestone 16 — final)
Current state: **Milestone 16 complete — final integration, security and
demo audit done. Quick Junction is feature-complete for the fast-track plan
(M10–M16) and demo-ready; the live MySQL database is on the current schema.**
See [Final state](#final-state-end-of-m16) at the end of this document.

> **Numbering note:** the Milestone 10 brief called itself "M08 — Restaurant
> Operations, Customer Dashboard, Table Reservation, Customer Order
> Cancellation & Sales Analytics". This document already has a Milestone 08
> (finalization), so the work is recorded as **Milestone 10** to keep history
> unambiguous.

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
| 06 | Customer preferences + deterministic recommendation engine | ✅ Complete |
| 07 | Local Qwen inference + hand-authored dataset + LoRA fine-tune | ✅ Complete |
| 07.1 | Dataset rebalance (60→150) + V2 retrain + 3-way evaluation | ✅ Complete |
| 08 | Finalization: Bootstrap UI, MySQL E2E, security audit, demo readiness | ✅ Complete |
| 09 | Security hardening: server-side session revocation | ✅ Complete |
| 10 | Customer cancellation, tables, reservations, order source, sales reporting (brief "M08") | ✅ Complete (MySQL-verified) |
| 11 | Payments, admin refunds, paid-order cancellation rule, automatic table release | ✅ Complete (live MySQL-verified) |
| 12 | Tax (configurable, snapshotted) + staff/admin discounts | ✅ Complete (not yet applied to live DB) |
| 13 | Inventory: stock, recipes, movement ledger, sale deduction | ✅ Complete (not yet applied to live DB) |
| 14 | Kitchen screen: queue, one-tap workflow, prep timestamps and timings | ✅ Complete (not yet applied to live DB) |
| 15 | UI/UX overhaul: design system, role nav, dashboards, admin payments + audit views | ✅ Complete (no schema change) |
| 16 | Final integration, security and demo audit | ✅ Complete |

**Test suite: 710 passing, 7 skipped (MySQL-only), 0 failing on SQLite; 716 passing, 1 skipped, 0 failing on MySQL 8.0.46** (end of M16; `pytest`; in-memory SQLite by default for
application tests, temporary on-disk SQLite for migration tests; the LLM is
disabled in testing and never loaded).
**MySQL verification: COMPLETE** (MySQL 8.0.46, head `2d9f3b20045f`) — see [MySQL verification status](#mysql-verification-status).

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

## Milestone 06 — Preferences + deterministic recommendation engine ✅

> **The recommendation engine is deterministic and does not use the Local
> Qwen model yet.** No LLM, no external API (OpenAI/Gemini/Anthropic/
> Ollama), no network access, no embeddings service, no vector database, no
> GPU. TF-IDF + cosine similarity, in-process.

### Implemented

**Preferences** — `app/services/preferences.py`, `app/routes/preferences.py`:

| Route | Auth | Notes |
| --- | --- | --- |
| `GET /preferences` | login required | own preferences only |
| `POST /preferences` | login required | CSRF-protected, enum-validated |
| `GET /recommendations` | login required | ranked menu for the session's user |

`CustomerPreference` (from M03) was reused as-is — **no schema change, no
migration in this milestone**. The three fields reuse `MenuItem`'s existing
`DietaryType` / `Cuisine` / `SpiceLevel` enums; no parallel enums were
created. All three are optional; "no preference" is stored as `NULL`. One
row per customer, located by the authenticated `user_id`.

**Engine** — `app/services/recommendations.py`:

1. Candidates = available items in active categories (same filter as the
   public menu).
2. **Dietary hard filter** applied before scoring.
3. Each item → a text document: name, description, category, cuisine,
   `spice_<level>`, `diet_<type>`, ingredients. **Price and database id are
   deliberately excluded** (price magnitude is meaningless to a
   bag-of-words model; an id carries no taste signal).
4. Preferences → a query document in the same vocabulary.
5. `TfidfVectorizer` + `cosine_similarity`; sorted descending, ties broken
   on name for stable output.
6. Score → `Strong` / `Good` / `Fair` / `Suggested` label for the UI.

**Order history — implemented.** Only `COMPLETED` orders count, weighted at
`HISTORY_WEIGHT = 0.5` (stated preference outranks past behaviour). History
is dietary-filtered before use, so it can neither surface nor amplify a
restricted item.

**Dietary safety rule** (`_DIETARY_COMPATIBILITY`): vegan → vegan only;
vegetarian → vegan + vegetarian; eggetarian → + eggetarian;
non-vegetarian → everything; nothing stated → everything. `vegetarian`
excludes `eggetarian` deliberately (stricter reading is safer).

**Dependency change:** `scikit-learn==1.7.2`, `scipy==1.15.3`,
`numpy==1.24.3` promoted from the AI stack into `requirements.txt`, since
`/recommendations` is a normal customer-facing page that must work where the
AI stack is absent. ~100 MB; rationale and swap-out path recorded in
`requirements.txt`.

### Tests run — 25 new, all passing

`tests/test_recommendations.py` covers all 21 required scenarios: preference
creation, update (same row reused), validation (service + HTTP), customer
ownership, TF-IDF document generation, cosine ranking, cuisine influence,
spice influence, dietary filtering, unavailable/inactive exclusion, empty
preferences, no-menu-items, real database ids, DB prices as `Decimal`, no
fabricated items, route authorization, CSRF, forged user id, completed-order
history influence, history-only-when-completed, dietary overriding history,
and determinism across repeated runs.

**Full suite: 142 passing, 0 failing** (foundation 10, auth 29, menu 26,
orders 28, migrations 4, staff orders 20, recommendations 25). No
regressions.

**Non-vacuity proven:** temporarily disabling the dietary hard filter made
**five** tests fail — including both history-override tests — confirming the
safety rule is genuinely asserted rather than incidentally true. Reverted.

### Security checks actually performed

Automated **and** re-verified in Chrome against the running dev server:

- **Unauthenticated** → `/recommendations` and `/preferences` returned 401;
  no preference row created.
- **Forged user id** → victim's id submitted as form field, query parameter
  and `X-User-Id` header while logged in as someone else: victim's row
  untouched, change landed on the attacker's own row. Database afterwards
  held exactly one preference row, for the authenticated user.
- **Invalid values** → `carnivore` / `klingon` / `volcanic` re-rendered the
  form (HTTP 200, not a redirect) and wrote nothing.
- **CSRF** → tokenless POST returned 400, nothing written.
- **Dietary bypass** → a vegetarian customer never saw the non-vegetarian
  item, on screen or in tests.
- **Unavailable items** → "Sold Out Curry", deliberately built to match the
  stated preferences perfectly on every attribute, was correctly absent.
- **Prices** → the three displayed prices (249.00 / 399.00 / 149.00) were
  compared against the database directly and matched; engine produces no
  monetary value at all.
- **Logs** → `logs/security.log` and `logs/app.log` contained zero
  occurrences of `password`, `argon2`, `csrf_token`, or `session`.
- **Static review** → no float arithmetic on money (the one `float()` is a
  cosine score), no string-built SQL, no `|safe` in the new templates, and
  `user.id` sourced only from `get_current_user()`.

### Migration status

**No migration was required or created.** `customer_preferences` has
existed since M03. The migration-consistency harness re-confirmed the
existing 4-revision chain still produces exactly the models' schema across
all 9 tables, and `tests/test_migrations.py` (4 tests) still passes.

---

## Milestone 07 — Local Qwen + hand-authored dataset + LoRA ✅

> The LLM **explains**; the deterministic engine **decides**. No external
> API, no paid service, no network at inference, no GPU, no API key.

### Environment correction (found first, before building anything)

`requirements-ai.txt` did not describe reality. It claimed `torch==2.13.0`
and `transformers==5.15.0` — **neither version exists** — and
`transformers`, `accelerate` and `safetensors` were **not installed at all**.
The documented "already verified to load locally on CPU" claim therefore
could not be reproduced as the repository stood. The file has been rewritten
against the actually-installed, verified environment.

`transformers >= 4.51` is a hard requirement: that release added the `qwen3`
architecture declared in `models/Qwen3-0.6B-Base/config.json`. Pinned 4.57.1.

**NumPy conflict resolved.** `requirements.txt` pinned `numpy==1.24.3` while
`requirements-ai.txt` pinned `2.2.6`, making the two files mutually
uninstallable. Both now agree on **1.24.3** — the already-installed version,
verified working with scikit-learn 1.7.2 / scipy 1.15.3 (recommendation
engine) *and* torch 2.10 / transformers 4.57.1. `requirements-ai.txt`
deliberately does not re-pin numpy, so the files cannot drift apart again.
Nothing was blindly upgraded: installs were run under a constraints file
pinning numpy/torch/scikit-learn/scipy, and the 142-test suite was re-run
after each install.

**Environment quirk:** this machine has a broken TensorFlow install (its
protobuf predates `runtime_version`) which `transformers` imports
opportunistically. `USE_TF=0` / `USE_FLAX=0` / `USE_JAX=0` are set before the
`transformers` import in both `local_llm.py` and `train_lora.py`. Without
them, importing `transformers` *or* `peft` raises `ImportError`.

### Dataset — hand-authored

| | |
| --- | --- |
| Source | **written by hand for this project** — not scraped, not downloaded, not LLM-generated |
| Raw | `data/raw/seed_examples.jsonl` — **60** examples |
| Train / validation | **50 / 10** |
| Categories | **10**, six each, 5 train + 1 validation per category |
| Split | deterministic, content-addressed (SHA-256), no RNG — byte-reproducible |

Documented in `data/README.md`; built by `scripts/build_dataset.py`, which
refuses to write on duplicates or split overlap.

### Training — a real run completed

`python training/train_lora.py --epochs 3` — **not** a smoke test:

| | |
| --- | --- |
| Method | LoRA (PEFT), rank 8, on q/k/v/o/gate/up/down projections |
| Hardware | **CPU only** (`torch 2.10.0+cpu`, `cuda_available=False`) |
| Steps | 39 (50 examples × 3 epochs, batch 1 × grad-accum 4) |
| Wall-clock | **1278 s ≈ 21.3 min** (~32 s/step) |
| Trainable | **5,046,272 / 601,096,192 = 0.84 %** |
| Loss | ~3.13 → **0.63** final step (mean 1.63) |
| Artifact | `models/qwen3-0.6b-quickjunction-lora/` — 20 MB adapter + `training_metrics.json` |

Metrics are written by the script itself, not transcribed by hand.

### Inference

`app/services/local_llm.py`, route `GET /recommendations/explain`.

Measured on CPU, no contention: base **19.5 s** first call / **8.9 s**
subsequent; fine-tuned **12.6 s** / **6.8 s**; ~7.6 tok/s raw. Model loads
lazily once per process (2.4 s) and **never in the test suite**.

`/recommendations` never invokes the model, so the deterministic path stays
fast regardless.

### Tests — 21 new

`tests/test_llm.py`: dataset schema, no duplicates across splits, split
reproducibility (re-runs the builder and diffs bytes), no secrets in the
dataset, model-path configuration, path cannot come from a client, prompt
built from validated facts only, LLM-disabled path, facts passed to the LLM,
hostile output cannot alter authoritative data, LLM failure does not break
recommendations, unavailable items unreachable, dietary-filtered items
unreachable, prompt-injection neutralisation, length bounding, no external
API/secrets in the AI code, training script offline, route authorization,
empty-menu handling.

**Full suite: 163 passing, 0 failing.** No regressions.

**Non-vacuity proven:** disabling `_sanitise()`'s character class made the
prompt-injection test fail; restored and re-verified.

### Security review performed

- Prompt injection via **menu text** — a test caught that stripping newlines
  alone was insufficient (a hostile item name could still emit
  `Customer preference:` inline). The sanitiser now also strips **colons**,
  because the prompt format is `label: value`. Free text capped at 120 chars.
- Prompt injection via **preferences** — not a free-text surface; enum
  values re-validated server-side and rendered from the enum.
- **Client-controlled facts** — the browser cannot reach the prompt; every
  field is built server-side from `MenuItem` rows.
- **Arbitrary model path** — no route or function accepts a path; asserted
  by signature inspection and a query-string attempt.
- **Secrets / network** — the module is grepped for `openai`, `anthropic`,
  `gemini`, `ollama`, `api_key`, `bearer`, `http://`, `https://`,
  `requests.post`, `urllib.request`; the training script likewise, plus
  `report_to=[]`.
- **Logging** — failures log a message only; never the prompt, preferences,
  or generated text.

### Browser verification — all 10 steps

Login → set preferences (indian/vegetarian/hot) → deterministic
recommendations correct with Butter Chicken excluded by the dietary filter →
explanation page showed the facts table (249.00, indian, vegetarian, hot,
38 %) **and** a grounded LLM sentence → marked Paneer Tikka unavailable →
it vanished from `/recommendations` **and** from the explanation page
entirely, top item correctly became Margherita Pizza → with the model path
broken, `/recommendations/explain` returned **200 in 0.02 s** showing
"AI explanation temporarily unavailable." with all deterministic facts
intact, and `/recommendations` was completely unaffected.

### Quality defects found (documented, not hidden)

The fine-tuned model produced two real errors during verification:

1. **Invented facts** — claimed an item was "the only item on the menu that
   meets all your requirements", which it was never told.
2. **Sycophantic agreement** — for a customer preferring `indian`/`hot`
   shown Margherita Pizza (`italian`, spice `none`), it wrote *"Margherita
   Pizza is Italian, which is what you prefer… It is not hot, which is what
   you prefer"*. Only the vegetarian clause was true.

Root cause of (2) is dataset balance: 60 examples dominated by strong-match
cases taught "assert everything matches" as a template. The fix is more
partial/failed-match examples and a re-train, not more of the same data.

**Contained by architecture:** the same page independently showed
`Cuisine: italian`, `Spice level: none`, `Match: 27%` and the customer's real
preferences — all correct, all contradicting the prose. Nothing operational
reads the prose. See `docs/AI.md` §5.

### Migration status

**No migration required or created.** M07 added no database table, column or
constraint. The 4-revision chain and `tests/test_migrations.py` are unchanged
and still pass.

---

## Milestone 07.1 — Dataset rebalance + V2 retrain ✅

> Fixes the one substantive quality defect shipped in M07 (known issue #20):
> the adapter asserted that mismatched items matched.

### Dataset V2 — hand-authored, rebalanced

| | v1 (M07) | v2 (M07.1) |
| --- | --- | --- |
| Examples | 60 | **150** |
| Categories | 10 | **15** |
| Train / validation | 50 / 10 | **120 / 30** |
| Mismatch-oriented share | low | **106/150 = 71%** |
| `strong_match` share | dominant | **10/150 = 6.7%** |

Written by hand for this project — not scraped, not downloaded, not
LLM-generated. v1 is **preserved unchanged** (verified byte-identical after
rebuild) as the record of what M07 trained on.

All 15 required categories present: strong/partial/weak match, cuisine /
spice / dietary / ingredient mismatch, multiple conflicts, unavailable items,
no recommendations, correct explanations, contradictory candidates, history
influence, preference-over-history, and why-not-match.

`contradictory_candidate` deliberately contains both verdicts (8 rejecting a
false claim, 2 confirming a true one) so the model does not learn to reject
everything — the opposite failure mode.

**Builder** (`scripts/build_dataset.py`) now takes `--version`, writes a
per-version `manifest.json` (dataset version, curation method, duplicate
policy, split method, counts, SHA-256 of every file), and still rejects
duplicates, split overlap and malformed rows. Split remains deterministic
and content-addressed; rebuilds are byte-identical.

### Training — a real second run completed

`python training/train_lora.py --dataset-version v2 --epochs 3`

**Exactly one variable changed from M07: the dataset.** Base model, LoRA
rank/alpha/dropout/targets, learning rate, batch size, grad-accum, epochs
and seed are identical.

| | M07 (v1) | M07.1 (v2) |
| --- | --- | --- |
| Optimizer steps | 39 | **90** |
| Wall-clock | 1278 s (21.3 min) | **3217 s (53.6 min)** |
| Final train loss | 1.63 | **0.984** |
| Trainable params | 5,046,272 (0.84%) | same |
| Hardware | CPU (`torch 2.10.0+cpu`) | same |
| Adapter | `models/qwen3-0.6b-quickjunction-lora/` | `models/qwen3-0.6b-quickjunction-lora-v2/` (20 MB) |

The training script now **refuses to overwrite an existing adapter**
(verified by attempting a v1 retrain — it exited non-zero). The M07
adapter's SHA-256 was recorded before M07.1 and re-verified after: unchanged.

### Evaluation — base vs M07 vs V2

`training/evaluate.py`, 8 held-out scenarios absent from both seed files
(asserted at startup). Greedy decoding; reproducibility confirmed by running
v2 twice and diffing every generation. Automated keyword/phrase scoring —
`must_not` (false match claims), `forbidden` (invented price/dish), `should`
(soft signal only). Full generations in `training/eval_results_m07_1.json`.

| System | Passed | Keyword coverage |
| --- | --- | --- |
| Base Qwen3-0.6B | 8/8 | 10/32 |
| M07 adapter (v1) | **5/8** | 12/32 |
| M07.1 adapter (v2) | **8/8** | **16/32** |

v1 failed exactly the three mismatch scenarios, reproducing the M07 defect
(claiming Italian "is what you prefer" to an Indian-preferring customer;
claiming an unspiced dish "matches all your preferences"; claiming a lamb
dish "matches all your dietary preferences" to a vegetarian). v2 passed all
eight.

**The base model's 8/8 is a scoring artefact, not quality.** Reading its
generations shows it evades the keyword checks while still being wrong — it
invented a preference the customer never stated, and described a
recommendation when zero items were available. Documented honestly in
`docs/AI.md` §11.3 rather than reported as "base is competitive".

### Success criteria from the brief — both met

| Case | v1 | v2 |
| --- | --- | --- |
| Indian+hot customer, Margherita Pizza (Italian, unspiced) | "Italian, **which is what you prefer** … best match" ❌ | "Italian **rather than Indian** … **not hot**" ✅ |
| Vegetarian customer, meat dish | "**matches all** your dietary preferences" ❌ | "**does not meet any** of your preferences … **not vegetarian**" ✅ |

### Integration decision — V2 promoted

V2 objectively improves the known failures (5/8 → 8/8) and regresses none,
so `config.py`'s `LLM_ADAPTER_PATH` now defaults to the v2 adapter. The M07
adapter remains on disk and is selectable via that variable. A missing
adapter falls back to the base model rather than failing (verified live).

### Tests — 33 new

- `tests/test_dataset_v2.py` (22): V2 schema, size, duplicates, split
  overlap, builder duplicate-rejection (guard actually fired), byte
  reproducibility, manifest SHA-256 correctness, v1 preservation, 15-category
  coverage, mismatch-ratio enforcement, negation presence, dietary-safety of
  the data itself, both-verdicts in contradiction category, evaluation
  held-out guarantee, evaluation dimension coverage, scorer determinism +
  catching the literal M07 sentence, separate adapter registry, M07 artefact
  preservation, overwrite guard, offline evaluation.
- `tests/test_llm.py` (+11): ten parametrised hostile prompt shapes plus the
  quotes-are-harmless case.

**Full suite: 196 passing, 0 failing.** No regressions.

**Non-vacuity proven:** sabotaging the dataset to be sycophantic made the
mismatch tests fail; restored and re-verified.

### Security re-verification

Prompt sanitiser re-tested against every shape the milestone names (colons,
newlines, instruction-like text, quotes, HTML, template syntax) — all
neutralised. Model paths remain configuration-only with no client input path.
No external API, no network calls, no secrets — grep-asserted in both
`local_llm.py` and the new `evaluate.py`.

### Browser verification

Logged in, set preferences (indian/vegetarian/hot), confirmed deterministic
recommendations (Paneer Tikka 48% strong, meat excluded by dietary filter),
and confirmed the V2 explanation appears alongside the facts table. Then
marked Paneer Tikka unavailable to reproduce the exact M07 failure scenario —
V2 produced *"Margherita Pizza is Italian rather than Indian … It is also
unspiced, so it is not hot"*, where V1 had claimed both were "what you
prefer". Unavailable and dietary-excluded items appeared nowhere. A broken
adapter path fell back to the base model; a missing model path degraded to
"AI explanation temporarily unavailable" in 0.03 s with all facts intact.

### Remaining defects in V2 — not fixed

V2 fixes the dangerous direction but is still not a good writer: it produced
*"Italian rather than Indian, so it does not meet your vegetarian
requirement"* (correct mismatch, non-sequitur conclusion — the pizza **is**
vegetarian), *"extra hot rather than extra hot"*, and confused availability
phrasing. Wrong in the conservative direction now (under-selling) rather than
over-selling. See `docs/AI.md` §11.5.

---

## Milestone 08 — Finalization, UI polish, MySQL E2E ✅

Finalization milestone: no new product features, no schema change, no
retraining. Bootstrap UI, verified MySQL operation, a live security audit, a
measured performance fix, and demo readiness.

### MySQL end-to-end — verified

Everything in [MySQL verification status](#mysql-verification-status) was
observed against a live **MySQL 8.0.46** server, `quick_junction`, Alembic
head **`38297b707b89`**. Highlights:

- All **15 `CHECK` constraints** present *and enforced* — direct `INSERT`s of
  a negative price, an invalid cuisine and an invalid role were each rejected
  by the server.
- Every money column `decimal(10,2)`; **zero** float/double columns anywhere.
- All **8 foreign keys** with the intended `ON DELETE` rule; all InnoDB.
- Full application flow exercised on MySQL (see Browser verification below).

**No schema change and no migration were needed in M08.**

### Bootstrap UI

Bootstrap 5.3.3 is **vendored locally** under `app/static/vendor/` (305 KB),
not loaded from a CDN — the project is offline-first, and a CDN would be the
only runtime dependency on the public internet. A test asserts no page
references a CDN host.

- New `app/templates/base.html`: responsive navbar, role-aware links, flash
  alerts, footer. **All 18 existing templates converted to extend it.**
- New partials: `_menu_card.html`, `_forms.html` (field/select/checkbox/
  submit macros), `_status_badge.html`.
- New `app/static/css/app.css` — 30 lines; everything Bootstrap already
  provides is used as-is.
- **New homepage at `/`** (`app/routes/main.py`). The site root previously
  returned **404** — a real gap for a demo.
- Order status badges are colour-coded; menu items are cards with clear
  prices, attribute badges and availability state; every list has a designed
  empty state; submit buttons disable and show a spinner on click.
- Accessibility: `visually-hidden` labels on icon-only controls, `scope` on
  table headers, `aria-label` on the nav toggle, breadcrumbs, and
  server-driven `is-invalid`/`invalid-feedback` validation messages.

Security properties held through the redesign, asserted by tests: no `|safe`
anywhere, a CSRF token in every POST form, hostile menu names escaped on
every page that renders them, no secret in any rendered page.

### AI presentation

`/recommendations/explain` now shows two clearly separated panels:

| Panel | Label | Content |
| --- | --- | --- |
| Left (green) | **Calculated by Quick Junction** | price, cuisine, dietary type, spice, match — each annotated "matches your preference" / "you prefer X" |
| Right (blue) | **Advisory** | the model's sentence, italicised, with a caveat that the left panel is authoritative |

If the model is unavailable the right panel shows "AI explanation
temporarily unavailable." and the left panel is unchanged.

### AI evaluation — re-run, no regression

`python training/evaluate.py` reproduced M07.1 **exactly** (greedy decoding,
identical generations):

| System | Passed | Keyword coverage |
| --- | --- | --- |
| Base Qwen3-0.6B | 8/8 | 10/32 |
| M07 adapter (v1) | 5/8 | 12/32 |
| **M07.1 adapter (v2, in use)** | **8/8** | **16/32** |

No quality regression, so **no retraining was performed**, per the brief.

Adapter behaviour verified live:

| Configuration | Result |
| --- | --- |
| V2 adapter (production default) | generates |
| V1 adapter (retained) | generates |
| No adapter configured | falls back to base model |
| Missing adapter directory | falls back to base model |
| Malformed model path | `None` → clean fallback message |
| Path-traversal attempt (`../../etc`) | `None` → no file access |

### Security audit — 36 live checks

Executed against the running application on MySQL, not from source reading:
**35 passed, 1 real finding.**

Authentication (wrong password rejected, session established, logout,
fixation rotation) · Authorization (customer→staff 403, customer→admin 403,
**staff→admin 403**, admin→both 200, anonymous 401, forged
`X-Role`/`X-User-Role`/`X-User-Id` ignored) · CSRF (valid accepted; missing
and forged rejected 400 on preferences, cart, checkout and staff status) ·
Input handling (invalid enums rejected without writing, negative quantity
rejected, nonexistent menu id rejected) · Disclosure (no hash in identity
JSON, no credential material on any page, no traceback on 401/404) · IDOR
(owner 200, other customer **404**, anonymous 401) · AI (deterministic facts
present, advisory label, no external endpoint).

**Finding — logout does not revoke a copied session cookie.** A `qj_session`
value captured *before* logout still authenticated afterwards (replay
returned `200` from `/orders` and `/account/me`). This is inherent to
Flask's stateless signed-cookie sessions: `session.clear()` clears only the
client's own copy. **`docs/SECURITY.md` §6 previously overstated this** as
"invalidating the old session immediately"; that claim has been corrected.
Bounded by `HttpOnly`/`Secure`/`SameSite`, the 8-hour lifetime, and the
per-request `User` reload. A real fix needs server-side session state (a
session table or a `token_version` column) — deliberately **not** added in a
finalization milestone. Pinned by
`tests/test_ui.py::test_no_server_side_session_revocation_exists`.

### Performance — one real N+1 found and fixed

| Page | Time | SQL queries |
| --- | --- | --- |
| `/` | 9 ms | 5 |
| `/menu` | 10 ms | 2 |
| `/cart` | 5 ms | 0 |
| `/orders` | 6 ms | 1 |
| `/staff/orders` | 9 ms | flat (eager-loaded since M05) |
| `/recommendations` | 18 ms | **14 → 5** |

`build_item_document()` reads `category` and `ingredients` for every
candidate, which lazy-loaded **one query per item** (9 separate `ingredients`
queries for a 9-item menu). `candidate_items()` and `build_history_document()
` now eager-load both. Pinned by
`tests/test_recommendations.py::test_23`, which builds a 12-item menu and
asserts the query count stays flat.

Model loading confirmed to happen **once per process**, not per request:
three consecutive explanation requests took 23 s, 9.2 s, 9.1 s.

### Browser verification — full demo workflow on MySQL

Homepage → register `demoguest` → log in → browse menu → set preferences
(vegetarian/Indian/hot) → recommendations (Paneer Tikka top at 34 %; meat
dishes absent entirely) → AI explanation (facts panel + advisory panel) →
add to cart (2 × 249.00 = 498.00) → checkout → **Order #2 PENDING** → log in
as `staff` → order queue → `PENDING → CONFIRMED → PREPARING → READY →
COMPLETED` → back as customer, status shows **COMPLETED** → log in as
`admin`, menu management renders with availability badges → log out.

Negative cases observed: invalid transition (`COMPLETED → PREPARING`) with a
**valid** CSRF token rejected, order unchanged · CSRF-less status change 400
· staff→admin 403 · customer→staff 403 · customer→another order 404 ·
unavailable item absent from the public menu · unknown route clean 404 with
no traceback · after logout all protected routes 401 while public pages 200.

### Tests — 221 passing

| File | Tests |
| --- | --- |
| `test_foundation.py` | 10 |
| `test_auth.py` | 29 |
| `test_menu.py` | 26 |
| `test_orders.py` | 28 |
| `test_migrations.py` | 4 |
| `test_staff_orders.py` | 20 |
| `test_recommendations.py` | 26 |
| `test_llm.py` | 32 |
| `test_dataset_v2.py` | 22 |
| **`test_ui.py` (new)** | **24** |

**221 passed, 0 failed, 0 skipped, 12 warnings** (all the pre-existing
Flask-SQLAlchemy `get_engine` deprecation in `migrations/env.py`).

Ten tests broke during the UI conversion because they assert on user-visible
copy. **No test was weakened**: the copy was adjusted so the original
assertions hold unmodified ("Your cart is empty." kept its period; the order
table renders "Order #N").

### Demo readiness

`scripts/seed_demo.py` — idempotent, refuses to run against a production
config. Creates 3 categories, 10 menu items (one deliberately unavailable)
and three accounts: `customer` / `staff` / `admin`, all
`demo-password-1`. README documents install, `.env`, migrations, tests, run,
and a five-minute demo path.

### AI positioning (accurate statement)

We did **not** train a large language model from scratch. We used
Qwen3-0.6B-Base as the foundation model, hand-authored a project-specific
instruction dataset, fine-tuned locally with parameter-efficient LoRA
(0.84 % of parameters), evaluated the adapter against held-out scenarios,
and integrated it as an explanation-only component.

---

## Milestone 09 — Session revocation hardening ✅

Single-issue hardening milestone. No product features, no retraining, no
recommendation or UI changes. It closes the one genuine vulnerability the M08
audit found.

### The vulnerability (root cause)

M08 verified live that a `qj_session` cookie **copied before logout still
authenticated afterwards** — replaying it returned `200` from `/orders` and
`/account/me` after the original browser had logged out.

Cause, confirmed by reading the code rather than assumed: the session was a
signed cookie carrying exactly one value, `user_id`, and `get_current_user()`
accepted any validly-signed cookie whose user existed and was active. There
was **no server-side state to invalidate**, so `session.clear()` on logout
could only clear the browser's own copy — never a copy someone else held. The
copy stayed valid for the full 8-hour lifetime.

### The fix

`users.session_version`, an integer counter:

| Step | Behaviour |
| --- | --- |
| `login_user()` | stamps the session with the user's current `session_version` |
| every request | `get_current_user()` compares session version against the column; mismatch → clear session, return `None` |
| `logout_user()` | increments the column, so every session issued before that logout stops validating |

A **counter, not a token** — nothing secret is stored in the column or placed
in the cookie, so there is no session secret to leak, hash or rotate. The
value is never logged and never appears in a response body.

`logout_user()` still clears the cookie even if the counter cannot be
committed, so the browser in hand is logged out regardless.

**Scope: logout revokes all of that user's sessions.** One counter cannot
distinguish sessions, so logging out on one device logs the account out
everywhere. Per-session revocation would need a session table or a per-session
token — a materially larger subsystem for a marginal usability gain, and
over-revoking is the safe direction. Pinned by
`test_9_logout_revokes_every_session_for_that_user` so it is an explicit
choice, not an accident.

**Fail-closed on upgrade:** sessions predating the migration carry no version
and are refused, so every pre-existing login is invalidated on deploy.

### Migration

One migration, `2d9f3b20045f` (`down_revision = 38297b707b89`), adding a
single column. Head is now **`2d9f3b20045f`**.

Verified **against live MySQL 8.0.46**: `upgrade` → column present as
`int NOT NULL DEFAULT 0` with existing rows backfilled to `0` → `downgrade` →
column removed → `upgrade` → head. `tests/test_migrations.py` still passes, so
the chain continues to reproduce the models' schema exactly.

### Verification — live, with controls

Every replay check is paired with a **control** replaying a cookie that has
*not* been revoked, so a broken harness cannot produce a false pass.

Against MySQL across separate processes (no shared application state):

| Step | Result |
| --- | --- |
| owner `/orders` before logout | 200 |
| CONTROL — replay captured cookie pre-logout | **200** (harness works) |
| owner `/orders` after logout | 401 |
| **REPLAY captured cookie after logout** | **401 — fixed** |
| replay `/account/me` after logout | 401 |
| fresh login | 200 |
| old cookie after fresh login | 401 |

### Tests — 18 new, 239 total

`tests/test_session_revocation.py`: logout invalidates the current session;
copied cookie cannot authenticate after logout (across `/orders`,
`/account/me`, `/preferences`); fresh login works; unauthenticated still 401;
forged/garbage cookies never authenticate; RBAC unaffected; a revoked *staff*
session loses staff access; forged role headers still ignored; CSRF still
enforced; no session material in logs or response bodies; login→logout→login
cycles stable with the counter incrementing; a failed login neither
authenticates nor revokes; session-fixation protection retained; deactivation
still immediate; one user's logout does not affect another user; multi-device
revocation semantics; and an end-to-end checkout smoke test.

**239 passed, 0 failed, 0 skipped.** No existing test was weakened.

**Non-vacuity proven:** disabling the version comparison makes exactly the
three replay tests fail (`test_2`, `test_5b`, `test_9`); restored and
re-verified.

The M08 placeholder test that asserted *no* revocation existed — written to
fail the moment one was added — did exactly that, and was replaced by
`test_session_revocation_state_exists`.

### Security regression audit — 36/36

The standing live audit re-run in full: authentication, authorization
(customer→staff/admin 403, staff→admin 403, admin→both 200, anonymous 401),
forged role headers, CSRF (missing and forged across four routes), input
handling, disclosure, IDOR (404 not 403), and the AI boundary.

**"old session cookie unusable after logout" moved from FAIL to PASS.** It was
the only failing check in M08; the audit is now 36/36.

### Browser verification

Login as customer → `/orders` 200 → logout → `/orders`, `/account/me`,
`/preferences` all 401 while `/menu` stays 200 → fresh login 200 → RBAC
re-checked per role: staff (queue 200, admin 403), admin (queue 200, admin
200), customer (both 403) → final logout 401.

`document.cookie` was confirmed **empty** in the browser: `HttpOnly` prevents
a page script from reading `qj_session` at all, which is precisely why the
capture/replay half of the test is performed at the HTTP layer instead.

### Finding — test-harness weakness (not a production issue)

While building the regression tests I found that the pytest `app` fixture
holds **one** application context open for the whole test, and Flask reuses an
already-pushed app context instead of creating one per request. `g.current_user`
therefore persists between requests *and across different test clients* within
a test — an anonymous client was observed being served as an authenticated
user, and a second `login()` silently no-opped because `/login` redirects when
someone is already signed in.

Production is unaffected: each real request pushes its own application
context, which is why the live MySQL verification above behaves correctly.

It does mean any test that switches identity mid-test can pass for the wrong
reason. The new tests clear the cache explicitly via `forget_cached_user()`,
documented at the call site. Auditing the older suites for the same latent
weakness is recorded as known issue #29 rather than attempted in a
single-issue hardening milestone.

---

## MySQL verification status

**MySQL verification is COMPLETE.** Performed in M08 against a live server;
every line below is an observed result, not a claim.

| | |
| --- | --- |
| Server | **MySQL 8.0.46** |
| Database | `quick_junction` |
| Driver | PyMySQL 1.2.0 |
| Alembic head | **`2d9f3b20045f`** (`flask db current` reports it as head; was `38297b707b89` before M09) |
| Tables | 10 — the 9 application tables plus `alembic_version` |
| Engine | InnoDB for every table |

**Verified properties that SQLite could never confirm:**

- **All 15 `CHECK` constraints exist natively** (`information_schema`), and
  are **enforced**: direct `INSERT`s of a negative price, an out-of-vocabulary
  cuisine, and an invalid user role were each rejected by the server.
- **Money columns are `decimal(10,2)`** — `menu_items.price`,
  `orders.subtotal`, `orders.total`, `order_items.unit_price_snapshot`,
  `order_items.line_total`. A scan of `information_schema.columns` found
  **zero** `float` or `double` columns anywhere in the schema.
- **All 8 foreign keys** carry the intended `ON DELETE` rule:
  `orders → users` RESTRICT, `order_items → orders` CASCADE,
  `order_items → menu_items` RESTRICT, `menu_items → categories` RESTRICT,
  `customer_preferences → users` CASCADE, `audit_logs → users` SET NULL, and
  both `menu_item_ingredients` keys CASCADE.

**Application flows exercised against MySQL** (not SQLite): registration,
login, logout, menu browsing, preferences, recommendations, cart, checkout,
customer order history, staff order queue, the full status workflow, admin
menu management, and AI explanation. Decimal handling, order snapshots,
transaction behaviour, audit logging, CSRF and authorization all behaved as
designed — see the M08 section for the specific observations.

No schema change was made in M08; no migration was added.

---

## Known issues

| # | Issue | Impact |
| --- | --- | --- |
| 1 | ~~MySQL never verified~~ — **closed in M08**: verified end to end against MySQL 8.0.46 | — |
| 2 | ~~Migrations not exercised by tests~~ — **closed in M05** by `tests/test_migrations.py`; note the application tests still use `create_all()` by design | — |
| 3 | Anonymous cart is discarded on login (`login_user()` clears the session for fixation protection) — no cart merge | user who builds a cart before signing in loses it |
| 4 | Checkout idempotency relies on the cart being cleared after success, not a dedicated idempotency key | concurrent double-submit under multiple workers |
| 5 | Rate limiter is in-process only (`dict` + lock) | ineffective across gunicorn workers/instances |
| 6 | Error handlers always return JSON, even for HTML routes | poor UX on an unexpected error in a browser |
| 7 | ~~No security headers~~ — **closed in M16**: CSP (no inline script), `X-Frame-Options: DENY` / `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, HSTS when cookies are Secure | — |
| 8 | Audit log retention not enforced in code | `audit_logs` holds IPs; needs a purge policy |
| 9 | `tests/__init__.py` exists solely to stop a stray `tests` package in site-packages from shadowing the local one | environment-specific workaround, not a project need |
| 10 | ~~Order status changes have no concurrency guard~~ — **closed in M10**: every transition is a conditional `UPDATE … WHERE status = <expected>` | — |
| 11 | `CANCELLED` now records who/when/why and customers can self-cancel (M10), but there is still no refund, restock or notification — no payment exists to refund | cancellation is not operationally complete |
| 12 | The staff queue is unpaginated and unfiltered — every order, every load | degrades once order volume is non-trivial |
| 13 | `migrations/env.py` uses `db.get_engine()`, deprecated in Flask-SQLAlchemy 3.1 (12 warnings in the suite) — pre-existing, from the Flask-Migrate template | breaks on Flask-SQLAlchemy 3.2 |
| 14 | The TF-IDF corpus is re-vectorised on every `/recommendations` request | fine at tens–hundreds of items; needs caching before a real catalogue |
| 15 | `scikit-learn` + `scipy` + `numpy` (~100 MB) are now runtime web dependencies for arithmetically small work | deployment image size; swap is contained to `app/services/recommendations.py` |
| 16 | TF-IDF is bag-of-words: spice matches exactly or not at all ("hot" is not treated as nearer "medium" than "none") | ranking is coarser than a customer might expect |
| 17 | Cold start — no preferences and no completed orders yields available items in name order at score 0.0 | new customers get no personalisation until they act |
| 18 | Ingredients contribute to matching only where an admin entered them | uneven signal quality across the menu |
| 19 | ~~numpy pin conflict between the two requirements files~~ — **closed in M07**; both now agree on 1.24.3 | — |
| 20 | ~~LLM asserts matches that are false (sycophancy)~~ — **fixed in M07.1**: dataset rebalanced 60→150, V2 adapter scores 8/8 vs V1's 5/8 on held-out mismatch scenarios | — |
| 20a | **V2 prose is still not customer-ready**: correct verdicts reached by garbled reasoning (e.g. "Italian rather than Indian, so it does not meet your vegetarian requirement" — the item *is* vegetarian). Now wrong in the conservative direction rather than over-selling | needs a larger base model and/or substantially more data before the wording is presentable unreviewed |
| 21 | Explanation generation takes 7–9 s on CPU and has no hard wall-clock timeout (only `LLM_MAX_NEW_TOKENS=48`) | a pathological generation could hold a worker |
| 22 | Each gunicorn worker would load its own 1.2 GB model copy (module-level singleton, per process) | multi-worker deployment needs a shared inference process |
| 23 | The LoRA adapter is git-ignored with the rest of `models/`; reproducible from the dataset in ~21 min | a fresh clone has no adapter until training is re-run |
| 24 | `USE_TF=0` is required because of a broken TensorFlow install in this environment | environment-specific; harmless but surprising |
| 25 | ~~No `Cache-Control` on authenticated pages~~ — **closed in M16**: every page served to a signed-in user is `Cache-Control: no-store` (+ `Vary: Cookie`); static assets unaffected | — |
| 26 | ~~Logout does not revoke a copied session cookie~~ — **closed in M09** via `users.session_version`; verified live on MySQL with controls, 18 regression tests | — |
| 26a | Logout revokes **all** of that user's sessions, not just the current device — a deliberate consequence of using one counter instead of per-session state | logging out on a phone signs the account out on a laptop too |
| 29 | **Test-harness weakness**: the pytest `app` fixture holds one application context open, so `g.current_user` persists across requests and clients within a test. Production is unaffected (one context per request), but identity-switching tests can pass for the wrong reason unless they clear it | older suites not yet audited for this |
| 27 | AI explanation takes ~9 s per request on CPU after the model is loaded (23 s including first load) | acceptable on its own route; would not be acceptable inline |
| 30 | ~~Migration `b5e2f8c41a07` not yet applied to live MySQL~~ — **closed**: applied to MySQL 8.0.46 and verified (see "M10 final verification") | — |
| 32 | Downgrading **all the way to base** fails on MySQL: pre-M10 revisions `abf997064564`, `8d5ba0171efe`, `d8f3bfd0e2a9` drop foreign-key-backed indexes before their tables in `downgrade()` (MySQL 1553). Upgrades and the M10 downgrade are unaffected; left unedited as historical migrations | only matters for a full teardown by downgrade; fix is deleting those `drop_index` lines (downgrade-only) if wanted |
| 31 | ~~No payment/refund/tax/discount records~~ — **payments and refunds closed in M11; tax and discounts closed in M12** | — |
| 34 | ~~M12–M14 migrations not applied to live `quickjunctiondb`~~ — **closed in M16**: backed up, applied, schema + data verified | — |
| 37 | Orders have no free-text "kitchen notes" field (e.g. "no onions"); the kitchen screen shows items, quantities, source/table and timings only | adding customer notes is a small follow-up if wanted |
| 38 | ~~M15 UI not reviewed in a browser~~ — **closed in M16**: customer, staff and admin walked through in Chrome on a seeded scratch DB; no console/CSP errors | — |
| 39 | Admin payments and audit views show the latest 200 rows, unpaginated; the audit view is filterable by event type only | fine for a demo; add paging/date filters if volume grows |
| 35 | Stock is checked at checkout but not reserved: two concurrent orders can both pass and the later completion takes stock negative (shown "short"). Recipes are read at completion time, not snapshotted per order | deliberate — a served order is never blocked; staff correct counts with an adjustment |
| 36 | Inventory records quantities only — no unit cost, so no inventory valuation or COGS/profit | valuation needs purchase prices, out of scope |
| 33 | ~~Migration `c3a9d7e21f58` not yet applied to live `quickjunctiondb`~~ — **closed**: applied and verified 2026-09-29 (see "M11 live MySQL verification") | — |
| 28 | No production deployment configuration (no WSGI service unit, TLS termination, or reverse-proxy config in the repo) | deployment is out of scope so far |

Full ranked pre-production list: `docs/SECURITY.md` §17.

---

## Not implemented — do not assume these exist

*(M10 added customer-initiated cancellation and a sales dashboard; the rest
of this list stands.)*
Payment gateway / card processing / any payment record · refunds · tax ·
discounts · profit/expense accounting · **AI chatbot / conversational assistant** (M07 built
explanation-only inference, not a chat surface) · advanced analytics ·
reporting dashboard · deployment configuration · styled frontend
(Bootstrap/JS — every page is plain unstyled HTML) · password reset ·
email verification · staff queue pagination/filtering.

`ai/`, `dataset/`, `training/` are intentionally empty placeholders.

---

## Exact current state (verified at end of M10)

| Item | Value |
| --- | --- |
| Tests | SQLite **710 passed, 7 skipped**; MySQL **716 passed, 1 skipped**; 0 failing (warnings: the pre-existing `env.py` deprecation only) |
| Test files | earlier rows as listed per milestone; M10 added restaurant_ops 43, sales 17, reservations 29, ops_migration 1, m10_end_to_end 1, mysql_concurrency 7 (MySQL-only, incl. M11/M12/M13 races); M11 added payments 20; M12 added pricing 32; M13 added inventory 25; M14 added kitchen 14; M15 added ui_overhaul 22; M16 added final_audit 8, demo_flow 1 |
| Running tests on MySQL | `TEST_DATABASE_URL=mysql+pymysql://…/quickjunction_test pytest` — must be a disposable `*_test` database; the fixtures refuse anything else |
| Migrations | **11 revisions, head = `f3a6d9b2e8c5`** (M10 `b5e2f8c41a07`; M11 `c3a9d7e21f58`; M12 `d7f1e3a9c2b4`; M13 `e5b8c1d4f7a2`; M14 kitchen timestamps). **Live `quickjunctiondb` also at `f3a6d9b2e8c5`** (M16) |
| Tables | 14: previous 9 + `restaurant_tables`, `reservations`, `payments`, `pricing_settings`, `stock_movements` |
| Models | 14 modules in `app/models/` |
| Services | 17 modules in `app/services/` (M10: `tables`, `sales`, `reservations`; M11: `payments`; M12: `pricing`; M13: `inventory`; M14: `kitchen`; M15: `dashboard`) |
| Blueprints | 14: health, main, auth, account, menu, admin_menu, admin_staff, cart, orders, staff_orders, preferences, tables, reports, reservations |
| Audit events | 31 |
| Local LLM | Qwen3-0.6B-Base + **v2** LoRA adapter (20 MB); CPU-only, offline, explanation-only. v1 adapter retained |
| Dataset | **v2: 150** hand-authored examples (120 train / 30 validation), 15 categories. v1 (60) preserved |
| Order statuses | 7 (`pending`, `confirmed`, `preparing`, `ready`, `served`, `completed`, `cancelled`) — `served` added in M10 |
| Order sources | 4 (`dine_in`, `takeaway`, `delivery`, `online`) |
| Table statuses | 5 (`available`, `occupied`, `reserved`, `cleaning`, `out_of_service`) |
| Reservation statuses | 4 (`confirmed`, `completed`, `no_show`, `cancelled`) |
| Roles | 3 (`admin`, `staff`, `customer`) |
| Sessions | signed cookie (user id + version); server-side revocation via `users.session_version` |
| Recommendation engine | deterministic TF-IDF + cosine similarity, in-process; **no LLM** |
| Frontend | Bootstrap 5.3.3, vendored locally (no CDN); 1 base layout + 3 partials + 19 pages |
| Database (tests) | in-memory SQLite; migration tests use temporary on-disk SQLite |
| Database (target) | **MySQL 8.0.46 — verified end to end in M08; head `b5e2f8c41a07` applied and verified in M10** |
| Runtime deps | pinned in `requirements.txt`; now includes scikit-learn/scipy/numpy |

---

## Milestone 07.2 — Dataset V3 authored and validated (**training not performed**) ✅

**Scope: dataset only.** No model was trained, no V3 adapter was created,
`training/train_lora.py` was not run, and no application behaviour changed.
`LLM_ADAPTER_PATH` still resolves to the V2 adapter. Full detail in
`docs/AI.md` §13.

### Why V3

The M07.2 design audit measured V2 against the prompt the app actually sends
(`app/services/local_llm.py::build_prompt`): only **13 %** of V2 examples used
the production prompt shape and only **7 %** used its exact instruction
string. V2 was fine-tuned largely on prompts production never sends.

Four of V2's fifteen categories were also **unreachable**:
`ingredient_mismatch`, `unavailable_items`, `no_recommendations` and
`preference_over_history` describe situations the application cannot produce —
`ExplanationRequest` is a frozen dataclass of eight fields with no price,
ingredients, availability or history, and the model is never invoked when
there are no recommendations.

### What was built

| | |
| --- | --- |
| Seed file | `data/raw/seed_examples_v3.jsonl` — **200 examples, 13 categories** |
| Prompt shape | **100 %** production shape and instruction string |
| Contrastive groups | **20 × 3 = 60**, all intact in training |
| Train / validation | **161 / 39** (3 per category) |
| Raw SHA-256 | `d1f136ca…d55f406` |
| Overlap with V1/V2 | **zero** |
| Rebuild | byte-identical, verified |

Every example is hand-authored. The `input` block is rendered from the
production template so its shape is guaranteed; every `output` sentence was
written by hand.

### Builder change — minimal and backward-compatible

Contrastive groups must not straddle the train/validation boundary, but group
members span different categories and validation is drawn per category. The
fix in `scripts/build_dataset.py` is an optional `group_id` field plus one
extra term in the ordering key, so grouped examples sort last and whole groups
stay in training; `build()` then asserts the outcome and refuses to write if
any group is split.

**Backward compatibility was proven, not assumed:** v1 and v2 carry no
`group_id`, so the ordering collapses to the original hash order. Rebuilding
both produced **byte-identical** `train.jsonl` and `validation.jsonl`. The
only v1/v2 change on disk is the `split_method` description inside their
manifests, which had to change because the builder's documented method did.

### Verification

**25 validation checks, all passing** — shape, vocabulary, reachability under
the dietary hard filter, sentence count, no invented attributes, no invented
context, unset preferences never claimed as matched, "all three" only when
literally true, `Suggested` never asserting a match, no cross-dimension
confusion, no duplicates, zero V1/V2 overlap.

**Non-vacuity proven for every check.** Each was re-run against a deliberately
corrupted dataset (injected price, fabricated order history, a match claimed
on an unset preference, a broken group, a copied V1 example) and every one
flipped to failing.

`tests/test_dataset_v3.py` carries 29 of these as permanent tests, 16 of them
independently re-verified as non-vacuous. **268 passed** (239 → 268), no
existing test weakened.

`test_7` fails if a V3 adapter appears or the training default changes, so the
"dataset only" boundary cannot be crossed silently.

### Known limitation

Whether V3 improves generation is **unknown and unmeasured** — no V3 model
exists. V3 also does not address the prose-fluency defect in `docs/AI.md`
§11.5, which is a capacity limit of a 0.6B model rather than a data problem.

---

## Milestone 07.3 — V3 evaluation and comparison ✅ (**V3 not promoted**)

Evaluation only: no training, no adapter change, no production configuration
change. Full detail in `docs/AI.md` §14.

**Verdict: KEEP V2 for now.** V3 is clearly better than V2 on the real
production task, but it regressed on one specific class that should be fixed
before it faces customers.

### Harness

`training/evaluate.py` and `training/eval_results_m07_1.json` were left
**untouched** as historical evidence. A separate harness was added because the
old one cannot load V3 and because four of its eight scenarios test situations
the application cannot produce (availability, order history, an exclusion
note, an empty candidate list).

New: `training/eval_scenarios_production.py`,
`training/evaluate_production.py`, `training/eval_results_m07_3.json`,
`tests/test_evaluation_production.py`.

25 held-out cases (16 scenarios + 3 contrastive groups × 3), rendered by the
real `build_prompt`, generated greedily at the production token limit, and
trimmed by production's own `_tidy` — so the scored text is what a customer
would see. Verified absent from all 386 dataset prompts with zero dish-name
reuse.

### Results

| System | Passed | Difference coverage | Avg s |
| --- | ---: | ---: | ---: |
| base | 17/25 | 5/19 | 5.43 |
| v1 | 9/25 | 8/19 | 6.17 |
| v2 | 16/25 | 13/19 | 5.51 |
| **v3** | **22/25** | **17/19** | **4.01** |

Contrastive groups — V3's design target: **v3 9/9**, v2 6/9, v1 3/9, base 4/9.

V3 eliminated every hallucination (v2: 4), every overclaim (v2: 3), the
"only item on the menu" invention (v2: 3), and the dietary-compatibility
error (v2: 1). It is also the most concise system at 19.2 words average
against v2's 30.0.

The base model's 17/25 is **flattering**: it writes third-person copy
(0/25 address the customer), names the dish in 1/25, and several "passes"
contain false claims the second-person-tuned detectors miss. On the
register-independent measure it is last at 5/19.

### Why V3 is not promoted

All three V3 failures cluster on **"not set"** handling — the one dimension
where it is worse than V2 (2 errors vs 0). It narrates unset preferences
instead of leaving them alone, once leaking the literal `not set` placeholder
as an item attribute. That is the target for a V4 dataset pass.

### Production safety

`DEFAULT_DATASET_VERSION` is `v2`; `LLM_ADAPTER_PATH` resolves to
`models/qwen3-0.6b-quickjunction-lora-v2`. **291 tests pass** (270 → 291).

---

## Milestone 07.4 — Dataset V4 authored and validated (**training not performed**) ✅

Dataset only: no training, no V4 adapter, no production change. Detail in
`docs/AI.md` §15.

### Target

M07.3 found V3 better than V2 everywhere except **"not set"** handling (2
errors vs 0). Measured against the V3 file: 31 of its 41 unset-involving
examples narrate the unsetness, and **none** models silently omitting an unset
dimension. A second defect: 16 examples have item cuisine `other` and **none**
verbalise it, so the model substituted a concrete wrong cuisine.

### Composition

**240 examples, 15 categories** = the **159 V3 examples with no unset
preference retained byte-for-byte** (they produced M07.3's wins, and all 20
contrastive groups live entirely inside them) + **81 newly authored** replacing
the 41 defective ones.

New categories: `unset_omitted_silently` (30), `unset_partial_match` (16),
`unset_none_set` (10), `cuisine_other_unnamed` (15); `low_signal_suggested`
rebuilt (13).

Four authoring rules: an unset dimension is never mentioned at all (57 of 67
unset examples, vs V3's 0); the all-unset case is the sole exception; the
literal `not set` never appears in any output; `other` is never named as a
concrete cuisine.

### Build and verification

`python scripts/build_dataset.py --version v4` -> **195 train / 45 validation**,
20 groups all in train, byte-identical rebuild. Registering v4 left v1/v2/v3
processed outputs **byte-identical**.

**29 validation checks, all passing and all proven non-vacuous** -- including by
reinserting V3's three actual failure sentences and confirming each is caught.
Zero overlap with V1/V2; overlap with V3 is exactly the 159 retained examples;
**zero overlap with the 25 M07.3 evaluation prompts**.

**315 tests pass** (291 -> 315); `tests/test_dataset_v4.py` adds 24.

### Still unknown

Whether V4 fixes the behaviour is unmeasured -- no V4 model exists. The
hypothesis is falsifiable: retrain on v4, re-run
`training/evaluate_production.py`, and check that the not-set class clears
without regressing the other axes.

---

## Milestone 07.5 — V4 LoRA training ✅ (**V4 trained, NOT promoted**)

Training only: no evaluation, no promotion, no production change. The
application still loads the **V2** adapter.

### Device — CPU (Intel iGPU acceleration unavailable)

A pre-flight audit checked whether this laptop's Intel Arc integrated GPU
could accelerate training. The hardware and driver are healthy — Intel Arc
(Meteor Lake, `PCI\VEN_8086&DEV_7DD5`), driver `32.0.101.8331` — but the
installed PyTorch is `2.10.0+cpu`, built with **`USE_XPU=OFF`**:

```
torch.xpu.is_available()  -> False
torch.randn(1, device="xpu") -> AssertionError: Torch not compiled with XPU enabled
```

Enabling XPU would mean force-reinstalling torch plus ~29 Intel SYCL/oneMKL
packages, replacing `mkl` and `intel-openmp` underneath the currently verified
CPU stack. That was judged too risky for an unproven gain on an iGPU with no
dedicated VRAM, so **nothing was installed** and training proceeded on CPU.

> **Intel iGPU acceleration unavailable in this environment. Training
> proceeded on CPU.** No GPU usage is claimed.

### Dataset

240 raw | **195 train / 45 validation** | 15 categories | 20 contrastive
groups, all entirely in training. Raw SHA-256 `be19d0fc…debd18`, unchanged
from M07.4.

### Command and result

```
python -u training/train_lora.py --dataset-version v4 --epochs 3
```

| | |
| --- | --- |
| Device | **CPU** (`device: "cpu"` in the metrics file) |
| Duration | **3693.97 s (61.6 min)** |
| Optimizer steps | **147** (195 × 3 ÷ grad-accum 4) |
| Loss | 3.0529 at the first logged step → **0.5306** final mean |
| Trainable | 5,046,272 / 601,096,192 (**0.8395 %**) |
| LoRA | r=8, alpha=16, dropout=0.05, bias none, CAUSAL_LM, 7 target modules |
| Optimiser | lr 2e-4, batch 1, grad-accum 4, seed 42 |

Hyperparameters were **not changed**; the 0.840 % trainable figure is
identical to V1, V2 and V3, which is the cheapest confirmation of that.

Metrics were read from `models/qwen3-0.6b-quickjunction-lora-v4/training_metrics.json`,
written by the script itself, not transcribed.

### Adapter

`models/qwen3-0.6b-quickjunction-lora-v4/` — 35 MB total, with a 20,236,472-byte
`adapter_model.safetensors`, `adapter_config.json`, `training_metrics.json` and
the tokenizer files. `checkpoints/` is empty, as `save_strategy="no"` intends.

### Integrity

V1, V2, V3 and the base model are **byte-identical** to their pre-training
hashes (compared before and after; the weight-file SHA-256 prefixes and mtimes
match exactly). Each earlier adapter's `training_metrics.json` still describes
its own run — V1 39 steps/50 examples, V2 90/120, V3 123/161, V4 147/195.

### Tests

**317 passed** (315 → 317). One M07.4 test failed on the expected transition:
`test_6_v4_is_a_dataset_only_and_nothing_is_promoted` asserted that no V4
adapter existed, a guard against *unauthorised* training. M07.5 authorised it,
so that premise expired. The guard was **re-pointed, not removed** — it now
asserts V4 is **not promoted** (checking both the training default and the
adapter the application actually loads), plus two new tests covering V4
artefact well-formedness and the non-overwriting of V1/V2/V3. Both promotion
paths were verified to fail the test when flipped.

### Production status

`DEFAULT_DATASET_VERSION = "v2"` · `LLM_ADAPTER_PATH` →
`models/qwen3-0.6b-quickjunction-lora-v2` · **V4 trained, NOT promoted.**

### Known limitations

- **V4 is unevaluated.** Whether it fixes V3's not-set regression is unknown;
  a lower training loss than V3 (0.5306 vs 0.5592) is *not* evidence of that,
  since the datasets differ.
- CPU-only training takes ~1 hour per run, which limits iteration speed.

---

## Milestone 07.6 — V4 evaluation and comparison ✅ (**V4 NOT promoted — V5 required**)

Evaluation only: no training, no dataset change, no production change.

**Verdict: the primary gate failed.** V4 improved on almost every axis, but it
did **not** fix the not-set regression it was built to fix. Production stays on
V2, and a V5 dataset iteration is required.

### Method

Same harness and same 25 held-out cases as M07.3, rendered by the real
`build_prompt`, generated greedily at the production token limit and trimmed by
production `_tidy` — identical treatment for every system, no per-system
prompting or scoring.

Two minimal changes to `training/evaluate_production.py`, neither touching
scoring logic: registered `v4` in `SYSTEMS`, and added V4's raw and processed
splits to the held-out corpus. The second closed a real gap — the guard
previously covered v1/v2/v3 only, so V4, the system under test, was the one
dataset never checked. It now verifies against **467** prompts (was 386) and
still confirms all 25 cases are absent.

`training/evaluate.py` and `training/eval_results_m07_3.json` were **not
modified**. Results written to `training/eval_results_m07_6.json`.

**Reproducibility:** base, v2 and v3 scored *identically* to M07.3
(17/25, 16/25, 22/25 with matching coverage and taxonomy), so every V4
difference is attributable to V4 alone.

### Results

| System | Passed | Difference coverage | Concise | Avg words |
| --- | ---: | ---: | ---: | ---: |
| base | 17/25 | 5/19 | 25/25 | 35.2 |
| v2 | 16/25 | 13/19 | 25/25 | 30.0 |
| v3 | 22/25 | 17/19 | 25/25 | 19.2 |
| **v4** | **23/25** | **19/19** | 25/25 | **18.5** |

| | base | v2 | v3 | **v4** |
| --- | ---: | ---: | ---: | ---: |
| HALLUCINATION | 6 | 4 | 0 | **0** |
| OVERCLAIM | 0 | 3 | 0 | **0** |
| LABEL_FACT_CONTRADICTION | 1 | 3 | 1 | **1** |
| DIETARY_COMPATIBILITY_ERROR | 0 | 1 | 0 | **0** |
| CROSS_DIMENSION_ERROR | 0 | 0 | 0 | **0** |
| GARBLED_PROSE | 1 | 2 | 1 | **0** |
| NOT_SET_ERROR | 0 | 0 | 2 | **1** |

### Primary gates — 5 of 6 met

```
NOT_SET_ERROR = 0                FAIL (1)
HALLUCINATION = 0                PASS
OVERCLAIM = 0                    PASS
DIETARY_COMPATIBILITY_ERROR = 0  PASS
CROSS_DIMENSION_ERROR = 0        PASS
contrastive tracking = 9/9       PASS
```

### The gate that failed

**On the four not-set cases, V4 scores 2/4 — exactly what V3 scored.** The
count moved 2 → 1 only because V4 fixed one case and broke a different one:

- **Fixed** `suggested_low_signal`. V3 said *"It is an Indian dish"* about an
  `other`-cuisine item; V4 says *"Fattoush Salad is not Indian and carries no
  heat, so it is only a general suggestion."*
- **Still broken** `unset_cuisine_and_spice`: *"…it is Mexican rather than **the
  cuisine you chose**. It carries **the medium heat you set**."* Neither
  preference was set — two invented preferences in one output.
- **Newly broken** `unset_all`, which V3 passed: *"This is a general suggestion
  as you have not chosen any preferences. It is Continental and vegetarian,
  **matching those you set**."* — self-contradictory within one output.

Counted by substance rather than by taxonomy label, **2 of V4's 3 failures are
not-set failures**; the scorer files one as `LABEL_FACT_CONTRADICTION` because
that rule matched first. They were not reclassified to flatter the result.

### What V4 genuinely improved

Difference coverage reached **19/19** — V4 names every real difference, which
no earlier system did. Contrastive tracking is **9/9 with 9/9 coverage** (V3:
9/9 with 8/9). Garbled prose fell to **zero**. The `other` cuisine defect from
V3 is fixed — V4 now says "not Chinese"/"not Continental" instead of inventing
a concrete cuisine. It is also the most concise system at 18.5 words.

### Scorer correction

Manual review of all 25 V4 outputs found one detector bug: the enumeration
check counted attribute *values*, so "extra hot" registered as two items
("hot" + "extra hot") and wrongly flagged the correct sentence *"…carries no
heat where you asked for extra hot"*. It now counts *dimensions*. Applied
uniformly to all four systems over the saved generations; it changed **only**
that one V4 case (22 → 23) and left base/v2/v3 untouched. It does not change
the gate outcome.

### Production status

`DEFAULT_DATASET_VERSION = "v2"` · `LLM_ADAPTER_PATH` →
`models/qwen3-0.6b-quickjunction-lora-v2` · **V4 evaluated, NOT promoted.**
All five model artefacts verified byte-identical before and after evaluation.

**318 tests pass** (317 → 318).

---

## Milestone 07.7 — V4 production hardening ✅ (**V4 PROMOTED**)

No training, no new dataset, no model weights touched. Detail in `docs/AI.md` §18.

**Production now runs `models/qwen3-0.6b-quickjunction-lora-v4`.**

### The problem and the change of approach

M07.6 left V4 at 23/25 with one class outstanding: it credited customers with
preferences they had never set. Three dataset iterations had failed to remove
it, so M07.7 stopped teaching and started checking. The application already
holds the answer -- `ExplanationRequest.preferred_*` is `None` exactly when a
preference is unset -- so a deterministic guard in
`app/services/local_llm.py` compares each generation against that truth,
**rejects** an unsupported one and renders a grounded replacement built only
from the prompt's own fields.

### Results (25 held-out cases, identical methodology to M07.3/M07.6)

| System | raw | **+ guard** | coverage | avg words |
| --- | ---: | ---: | ---: | ---: |
| base | 17/25 | 17/25 | 5/19 | 33.0 |
| v2 | 16/25 | 19/25 | 14/19 | 27.1 |
| v3 | 22/25 | 24/25 | 18/19 | 18.4 |
| **v4** | 23/25 | **25/25** | **19/19** | **17.7** |

Every failure class at zero for V4 + guard; contrastive 9/9. All primary gates
met. V2 **with** the guard still scores 19/25 and keeps 3 hallucinations, 3
overclaims and a dietary error, so V4 is what makes production safe -- the
guard alone would not have been enough.

### Real application testing found what the harness did not

Twelve scenarios through the live app on MySQL 8.0.46 exposed a defect the
25-case harness never produced: the model paraphrases the placeholder as the
single word `unset` ("its heat is hot rather than unset"). The guard was
corrected to judge placeholder usage per sentence -- attribute position versus
a statement about the customer -- and all twelve scenarios are now clean.

### Tests

**318 before → 378 after**, all passing. `tests/test_preference_safeguard.py`
adds 60. Three pre-existing guards asserted "V4 not promoted"; that premise
expired with the authorised promotion, so they were **re-pointed, not
removed** -- V4 is now pinned as production *together with* the safeguard,
since promotion was conditional on it.

### Integrity

V1, V2, V3, V4 and the base model byte-identical throughout; datasets
unchanged; `training/evaluate.py` and the M07.1/M07.3/M07.6 result files
preserved.

---

## Milestone 10 — Restaurant operations, reservations, cancellation, sales ✅

Brief title: "M08 — Restaurant Operations, Customer Dashboard, Table
Reservation, Customer Order Cancellation & Sales Analytics" (see numbering
note at the top). Built by **extending** the existing order, audit, RBAC and
UI architecture — no second order, audit, sales or user system.

**Baseline before changes: 497 passing.** After: **587 passing, 0 failing.**

### Order lifecycle

```
PENDING   -> CONFIRMED | CANCELLED
CONFIRMED -> PREPARING | CANCELLED
PREPARING -> READY     | CANCELLED      (staff/admin only)
READY     -> SERVED    | COMPLETED      (SERVED new in M10: dine-in step)
SERVED    -> COMPLETED
COMPLETED, CANCELLED -> terminal
```

`served` was **added**, not substituted: `READY -> COMPLETED` is kept for
takeaway/delivery and so no existing flow changed.

**Concurrency guard (closes known issue #10).** Every transition — staff and
customer — is one conditional `UPDATE orders SET … WHERE id = ? AND status IN
(<expected>)`, and the caller checks `rowcount`. A customer cancel racing a
cook's "start preparing" is serialised by the database (InnoDB row lock);
exactly one matches and the other is rejected with "changed by someone else".
`PREPARING + CANCELLED` or any lost update is impossible.
(`app/services/orders.py::_apply_transition`)

### Customer order cancellation

`POST /orders/<id>/cancel` (`login_required`, CSRF). Rules, all server-side:

| Case | Result |
| --- | --- |
| own order, `pending`/`confirmed` | cancelled (302) |
| own order, `preparing`/`ready`/`served`/`completed` | 409 (JSON) / flash (HTML), unchanged |
| already cancelled | 409 "already cancelled" |
| someone else's order, or nonexistent | **404** (no existence leak, same as M04 IDOR rule) |
| anonymous | 401 |
| reason > 255 chars | rejected, unchanged |

Ownership is enforced twice — `get_order_for_user` in the route and
`user_id = ?` inside the UPDATE. Staff cancellation (including from
`PREPARING`) is unchanged; customers cannot reach `/staff/*` (403).

**Cancellation record** — new nullable columns on `orders`:
`cancelled_at` (DB clock), `cancelled_by_id` (FK users, RESTRICT),
`cancellation_actor` (`customer`/`staff`/`admin`, from the actor's server-side
role), `cancellation_reason` (≤ 255, optional). Audit reuses the existing
`order_status_changed` / `order_status_change_rejected` events, now with an
`"actor"` key in metadata. Orders cancelled before M10 keep NULLs — who
cancelled them was never recorded.

### Restaurant tables

`RestaurantTable` (`restaurant_tables`): unique `name`, `capacity`
(CHECK 1–50), `status` (CHECK-constrained enum), timestamps.

```
AVAILABLE      -> OCCUPIED | RESERVED | OUT_OF_SERVICE
RESERVED       -> OCCUPIED | AVAILABLE | OUT_OF_SERVICE
OCCUPIED       -> CLEANING | AVAILABLE
CLEANING       -> AVAILABLE | OUT_OF_SERVICE
OUT_OF_SERVICE -> AVAILABLE
```

| Route | Role |
| --- | --- |
| `GET /staff/tables` — board: status counts, each table's newest active order with items + total | STAFF, ADMIN |
| `POST /staff/tables/<id>/status` | STAFF, ADMIN |
| `GET/POST /admin/tables/new`, `/admin/tables/<id>/edit` | ADMIN |

Audit: `table_created`, `table_updated`, `table_status_changed`.

### Order source / table association

`orders.source` (`dine_in`/`takeaway`/`delivery`/`online`, NOT NULL, existing
rows backfilled `online` — the web checkout was the only channel) and
`orders.table_id` (FK, RESTRICT). Rule enforced in
`resolve_order_channel()` **and** by `ck_orders_source_table`:
dine-in **requires** a table; every other source **must not** have one.
Dine-in rejects CLEANING/OUT_OF_SERVICE tables and marks the table OCCUPIED.
Checkout has an order-type radio + table select. **History is preserved:**
`table_id` is never cleared, so a completed order still shows its table after
the table is freed (tested).

### Reservations

`Reservation` (`reservations`) — separate from `Order`. Columns: `user_id`,
`table_id`, `reservation_date`, `reservation_time`, `guest_count`, `status`,
`holds_slot`, `reserved_at`, `updated_at`.

**Ownership.** `Authenticated user → reservation.user_id`. The form has no
owner field; routes pass `get_current_user()`; a POSTed `user_id` /
`customer_id` / `X-User-Id` is never read (tested). Customer lookups are
ownership-scoped in the query → someone else's reservation is **404**.

**Timestamps.** `reservation_date` + `reservation_time` = the requested
booking slot (restaurant wall-clock). `reserved_at` = when the booking was
made, `server_default=now()` — never client-supplied. `reserved_at` doubles as
the row's creation time, so there is no separate `created_at`.

**Slot rule.** No slot policy existed, so the smallest deterministic one:
fixed, non-overlapping **2-hour slots starting 11:00, 13:00, 15:00, 17:00,
19:00, 21:00**. One reservation = one table × one slot. Any other time
(e.g. 19:30) is rejected, so "overlapping time" and "same slot" coincide.
Bookings must be in the future (DB clock) and ≤ 60 days ahead; guests
1..table capacity; OUT_OF_SERVICE tables cannot be booked.

**Double-booking strategy.** Enforced by the database, not by
check-then-insert: `UNIQUE uq_reservations_table_slot (table_id,
reservation_date, reservation_time, holds_slot)`. `holds_slot` is TRUE while
the booking is live and NULL once cancelled; NULLs are distinct in unique
indexes on both MySQL and SQLite, so cancelling frees the slot but two live
bookings can never both commit. The service simply inserts and turns the
IntegrityError into "just been booked". A test with **two real threads on
two connections** booking the same slot yields exactly one success.

**Table state vs reservation state.** `restaurant_tables.status` is *current*
floor state only. Future availability (`available_tables(date, slot,
guests)`) looks at slot bookings, capacity and OUT_OF_SERVICE — it ignores
OCCUPIED/CLEANING (a table busy now can be booked for tonight), and a booking
never changes a table's current status. The `reserved` table status means
"being held on the floor right now", set by staff.

**Reservation lifecycle:** created `CONFIRMED` (availability is checked at
booking, so no approval step) → `COMPLETED` | `NO_SHOW` | `CANCELLED`
(staff); customer may cancel own `CONFIRMED` bookings before the slot starts.
Transitions use the same conditional-UPDATE pattern as orders.

| Route | Role |
| --- | --- |
| `GET /reservations/new?reservation_date&reservation_time&guest_count` — slot search, free tables | CUSTOMER |
| `POST /reservations` → confirmation page (table, date, time, guests, reserved by, reserved at, status) | CUSTOMER |
| `GET /reservations`, `GET /reservations/<id>`, `POST /reservations/<id>/cancel` | CUSTOMER (own only) |
| `GET /staff/reservations` (today onward), `POST /staff/reservations/<id>/status` | STAFF, ADMIN |

Staff see the customer's **username only** — the existing privacy convention
from the order queue (no email). Audit: `reservation_created`,
`reservation_status_changed` (with `actor`).

### Customer dashboard

Every login already lands on `/account/` (pinned by
`test_staff_approval::test_10b`), which was the role-aware landing page. For
customers it now **is** the customer dashboard: tiles (recommendations,
preferences, menu, book a table, my reservations, my orders) plus recent own
orders with a "can cancel" hint, upcoming own reservations, and tables free
right now (with a note that later availability is a separate search). Staff
and admin pages are unchanged (tested). No login routing changed.

### Sales reporting

`GET /admin/reports` (**ADMIN only**; staff and customers 403), query-string
filters: `range=today|week|month|last_month|custom&start=&end=` or
`month=YYYY-MM` (monthly statement). No sales table — every figure is
recomputed from `orders` (`app/services/sales.py`).

**Formulas.** No payment, tax, discount or refund data exists in the system
(`total == subtotal` since M04), so:

| Figure | Definition |
| --- | --- |
| Recognised sale / "paid order" | order with status `completed` (the bill is settled at completion; no payment record exists) |
| Gross sales | Σ `subtotal` of recognised sales (checkout-time snapshots, never current menu prices) |
| Discounts | 0.00 — not recorded; labelled "not recorded" |
| Tax | 0.00 — not recorded; labelled "not recorded" |
| Refunds | 0.00 — nothing is ever collected before completion, so nothing to refund |
| Net sales | gross − discounts − refunds |
| Collected | Σ `total` of recognised sales |
| Average order value | net ÷ recognised order count (0 when none) |

Cancelled orders are **never** sales; pending/confirmed/preparing/ready/served
are counted as pending. The brief's example (1,000 + 1,500 completed, 2,000
cancelled) reports **2,500**, tested. Labelled revenue, **not profit** — no
cost data exists.

Dashboard: today's and this month's sales, net sales and AOV for the range,
orders placed / paid / cancelled / pending, financial breakdown, sales by
order type (count, net, %), current table-status counts, daily breakdown with
trend bars, sales by table (all tables, including zero rows; only dine-in
orders carry a table), and sales by hour.

**Dates.** Grouped by `created_at` (when the order was placed). "Today" comes
from the **database clock** (`app/utils/clock.py`) — the same clock that
stamps `created_at` — so no second timezone system. Ranges are
`[start 00:00, day-after-end 00:00)`; boundary tests cover 23:59:59 and
00:00:00 across a month end. Custom ranges ≤ 366 days; bad input falls back
to this month with a message.

### Migration

**`b5e2f8c41a07`** (`down_revision = 7c4e1a9b52d3`), hand-written: creates
`restaurant_tables` and `reservations`; adds the six `orders` columns, two
FKs, `ix_orders_table_id`, `ix_orders_created_at` and three CHECKs; widens
`ck_orders_status` for `served` and `ck_audit_logs_event_type` for 5 events.
Downgrade maps `served` → `ready`, deletes the 5 new audit event rows (same
policy as earlier revisions) and drops everything; no order row is lost.

Verified: `tests/test_migrations.py` (migrated schema == models across all 11
tables, round trip) and `tests/test_ops_migration.py` (pre-existing orders
backfilled `online`/NULL, DB rejects dine-in without table, `served`
accepted, downgrade keeps both orders). **Applied to live MySQL 8.0.46 and
verified** — see "M10 final verification" below.

### Indexes

`ix_orders_created_at` (report ranges), `ix_orders_table_id` (board, table
report), `ix_reservations_user_id` (my reservations),
`ix_reservations_date_time` (staff list, availability); the unique slot index
also serves table lookups. `orders.source`/`status` not indexed — reports
already filter by date first. Staff queue now eager-loads `Order.table`.

### Tests — 90 new

| File | Tests | Covers |
| --- | ---: | --- |
| `test_restaurant_ops.py` | 43 | customer cancel matrix, ownership, 401/404/409, CSRF, reason length, UI button, staff/admin cancel recorded, lifecycle, **3 race tests**, tables CRUD/capacity/status + DB CHECKs, source/table rules + DB CHECK, historical table, table RBAC + audit, board |
| `test_sales.py` | 17 | cancelled ≠ sales, gross/discount/tax/refund/net/collected/AOV, daily + monthly, presets, invalid ranges, hourly, by source/table, historical prices, month/midnight boundaries, served = pending, RBAC, "not profit" |
| `test_reservations.py` | 29 | owner/slot/`reserved_at`, same/different table & slot, off-slot time, DB unique constraint, cancel frees slot, capacity/guests, out-of-service, date/time validation, slot-aware availability, staff transitions, **threaded concurrent booking**, owner spoofing, IDOR, CSRF, 409, RBAC, username-only staff view, dashboard own-data-only, staff/admin dashboards unchanged, `served` step |
| `test_ops_migration.py` | 1 | migration against existing data, round trip |

One existing test changed: `test_foundation.py` pins the set of tables and
now includes the two new ones. No assertion was weakened.

**Non-vacuity proven:** dropping the status condition from the conditional
UPDATE fails exactly the three race tests; dropping
`uq_reservations_table_slot` fails the four double-booking tests (including
the threaded one). Both restored.

**Harness finding:** Flask-SQLAlchemy picks `StaticPool` (one shared
connection) whenever the configured URI is in-memory at `create_app` time, so
overriding `app.config["SQLALCHEMY_DATABASE_URI"]` afterwards still shares one
connection across threads. The concurrency test patches `TestingConfig`
*before* `create_app` and asserts the pool is not `StaticPool`.

### Security review (new code)

Every new route has `require_role`/`customer_required`/`login_required`;
all POSTs are CSRF-protected (tested on cancel routes); no identity is read
from form/args/headers; IDOR returns 404 for orders and reservations; enum and
numeric inputs validated in services and backed by DB CHECK/UNIQUE; no `|safe`
in new templates (reasons rendered escaped); no PII beyond username on staff
views; no new secrets, debug code or dependencies.

### Known limitations (M10)

- No payment, tax, discount or refund data — those rows read 0.00 until a
  payments milestone adds real records (issue #31).
- Table status is not auto-freed when a dine-in order completes; staff move
  it OCCUPIED → CLEANING → AVAILABLE on the board.
- Fixed 2-hour slots; no walk-in/reservation reconciliation (a booked table
  is not blocked from dine-in checkout at that time).
- Staff cannot place orders on a customer's behalf (no POS); dine-in orders
  come from customer checkout.
- Report and "today" dates follow the database clock (UTC on SQLite, the
  server zone on MySQL) — correct as long as the MySQL server runs in
  restaurant-local time.
- `scripts/seed_demo.py` does not seed tables yet (an edit was declined during
  the milestone); an admin adds them at `/admin/tables/new`.

### M10 final verification — real MySQL (2026-09-29) ✅ production-verified

Performed against the project's live **MySQL 8.0.46** database
(`quickjunctiondb`, REPEATABLE-READ, server clock UTC+05:45), not SQLite.

**Migration.** Live DB was at `7c4e1a9b52d3` (5 users, 2 orders, 18 menu
items). A `mysqldump --single-transaction` backup was taken first (kept
outside the repo). `flask db upgrade` applied `b5e2f8c41a07` with **no
error** — including `ck_orders_source_table` alongside the `table_id`
`ON DELETE RESTRICT` foreign key, the combination previously only reviewed
offline. `flask db current` → **`b5e2f8c41a07 (head)`**. `flask db check`
reports only the long-standing enum-CHECK autogenerate false positive
(documented since M03/M04) — no column, table, index or FK drift. Existing
data intact: both orders backfilled `online`/no table, counts and
`order_items` price snapshots unchanged.

**Schema (from `information_schema`).** `restaurant_tables` and
`reservations` present, InnoDB; every M10 column with the expected type and
default; CHECKs `ck_restaurant_tables_capacity_range`, `…_status`,
`ck_reservations_guest_count_positive`, `ck_reservations_status`,
`ck_orders_source`, `ck_orders_cancellation_actor`, `ck_orders_source_table`,
`ck_orders_status` (now includes `served`); FKs all `RESTRICT`; indexes
`uq_reservations_table_slot` (unique), `ix_reservations_user_id`,
`ix_reservations_date_time`, `ix_orders_table_id`, `ix_orders_created_at`.

**Direct constraint probes — 21/21 as expected**, inside one transaction
that was rolled back (0 rows left): rejected dine-in without table,
takeaway/delivery/online with table, capacity 0 and 51, duplicate table
name, invalid table/order status, unknown source, nonexistent table (FK),
guest count 0, deleting a table referenced by orders, and a **duplicate live
reservation for the same slot (1062)**; accepted valid rows, `served`, and
**two cancelled (`holds_slot` NULL) rows for the same slot** — confirming
MySQL treats NULLs as distinct in the unique slot index.

**Full suite on MySQL** (`TEST_DATABASE_URL` → a disposable
`quickjunction_test` database; the live DB is never used by tests):
**589 passed, 1 skipped, 0 failed**, 18 warnings, 248 s. SQLite:
**588 passed, 2 skipped** (the MySQL-only race tests), 24 warnings. All
warnings are the pre-existing `migrations/env.py` deprecation (#13).

The first MySQL run had 7 failures, fixed by category:

| Category | Failure | Fix |
| --- | --- | --- |
| **A — real bug** | `b5e2f8c41a07.downgrade()` dropped `ix_orders_table_id` and `ix_reservations_user_id` while foreign keys still needed them → MySQL 1553; a real `flask db downgrade` would have stopped half-way (MySQL DDL is non-transactional) | drop FKs before indexes; drop `reservations` without dropping its indexes first. Upgrade path unchanged, so the live DB (upgraded before the fix) is identical. |
| B — MySQL-specific | PyMySQL reports CHECK violations (3819) as `OperationalError`, SQLite as `IntegrityError` (`test_menu::test_7b`, 2 M10 tests, `test_ops_migration`) | `rejected_by_check_constraint()` in `tests/conftest.py`: accepts either class **only** if the message is a CHECK violation, so a lost connection cannot pass |
| C — test infra | `test_foundation` asserted tests never use MySQL | now asserts the stronger rule: a MySQL test DB must be a `*_test` database and never the app's `DATABASE_URL` — also enforced in the `app` fixture, so `drop_all()` can never reach the live DB |
| C — test infra | migration tests share one MySQL database (each gets a fresh in-memory DB on SQLite), so one test's leftovers broke the next | autouse fixture wipes the disposable test DB before migration tests on MySQL |
| pre-existing | full downgrade-to-base fails on MySQL in **pre-M10** revisions (see #32) | not changed (historical migrations); `test_downgrade_and_reupgrade_are_reversible` skips on MySQL only, with the reason; still runs on SQLite. M10's own round trip is tested on MySQL by `test_ops_migration` |

**Concurrency on MySQL** (`tests/test_mysql_concurrency.py`, runs only
against MySQL): two threads, two pooled connections, barrier-synchronised,
15 rounds per test, stable over 5 consecutive runs (75 + 75 contested races):
- customer cancel vs staff start-preparing — **exactly one wins every
  round**, final row is either `cancelled` with metadata or `preparing` with
  none; never both. Non-vacuity: with the status condition removed from the
  UPDATE, both succeeded (`['cancel', 'prepare']`) and the test failed.
- same table/date/slot booking — **exactly one `ok`, one clean
  `ReservationError` conflict**, exactly one live reservation.
- Finding (test-only): a MySQL REPEATABLE-READ session that has already read
  keeps its snapshot, so verification reads must end their transaction first
  (`expire_all()` is not enough). The application is unaffected — the
  conditional UPDATE and unique index use InnoDB's locking current read, and
  services commit before refreshing.

**End-to-end** (`tests/test_m10_end_to_end.py`, passes on MySQL and SQLite):
customer logs in → lands on `/account/` dashboard → sees free tables →
slot-aware search → books T01 (a forged `user_id` for another customer is
ignored) → confirmation shows owner + `reserved_at` → listed under My
reservations → another customer's reservation is 404 → dine-in order at T02
with correct source/table → cancels while pending (actor/by/reason/time
recorded) → staff board shows the active order, reservations list shows
usernames only (no email) → staff start preparing another order → customer
cancel of it is **409** and the order stays `preparing` → valid table
transitions succeed, `available → cleaning` is refused → staff get 403 on
reports → admin report for today: 2 completed sales = ₹1540.00, 3 placed,
1 cancelled excluded, by source (dine-in 1, takeaway 1), by table (T01 1,
T02 0), daily and hourly rows correct. Historical safety: menu repriced
385 → 999 leaves the completed order at ₹770.00 with its 385.00 snapshot and
table T01; cancellation metadata intact; a cancelled reservation keeps its
row but frees its slot, which is then rebooked.

**Security review.** Every M10 route carries `require_role` /
`customer_required` / `login_required`; no route reads a user id or role from
form, args, JSON or headers (grep-verified; spoofing tested); IDOR returns
404 for orders and reservations; customer cancel is ownership-scoped inside
the UPDATE; staff views show username only; all SQL is ORM or bound
`sa.text` parameters (the one f-string, in the test wipe fixture, interpolates
table names read from the server); no `|safe`; no secrets in source (the
backup and DB password never printed or committed); state transitions are
single conditional UPDATEs committed atomically. `.env` runs
`APP_ENV=development` as MySQL `root` — acceptable locally, refused by
`ProductionConfig` (see `docs/MYSQL_SETUP_HANDOFF.md` §5).

**Verdict: M10 is production-verified on MySQL**, with the limitations
listed under the M10 section and known issues #31–#32.

---

## Milestone 11 — Payments, refunds, automatic table release ✅

Baseline before changes: **588 passed, 2 skipped** (SQLite). Built on the
existing order, audit, RBAC and reporting code — no second order or sales
system; the report still derives every figure from `orders` + `payments`.

### Payment model

`Payment` (`payments`): `order_id` (FK orders, RESTRICT, **UNIQUE** — one
payment per order), `method` (`cash`/`card`/`wallet`, CHECK), `amount`
(`Numeric(10,2)`, CHECK > 0), `refunded_amount` (default 0, CHECK
`0 ≤ refunded_amount ≤ amount`), `captured_at` (DB clock), `recorded_by_id`
(FK users), `refunded_at`, `refund_reason` (≤ 255; latest), `updated_at`.
Every refund is also an audit row carrying its own amount, so the history
is complete even though the row keeps one cumulative total.

**Scope decisions.** Payment is taken at the counter by staff — there is no
card gateway (offline-first; no card data is stored). One payment per order;
split bills and tips are out of scope. The amount is always the order's
server-side `total` — a client-posted `amount`/`total` is ignored (tested).
**Tax and discounts are still not recorded** and stay 0.00 "not recorded" in
reports: no rule for them exists in the project, and inventing a rate would
be invented accounting. They need a decision (e.g. VAT %, service charge,
discount policy) before they can be built.

### Rules

| Rule | Enforcement |
| --- | --- |
| Pay any non-cancelled order once | `record_payment` locks the order row (`SELECT … FOR UPDATE`), re-reads status + payment; UNIQUE `order_id` catches a concurrent second insert |
| A cancelled order cannot be paid | same locked re-read |
| **A paid order cannot be cancelled until fully refunded** (staff or customer) | `NOT EXISTS (unrefunded payment)` inside the cancel UPDATE in `orders._apply_transition` — same order row the payment path locks. Customers see no cancel button once paid and are told to ask staff |
| Refunds are ADMIN-only, partial or full, never above the amount | conditional `UPDATE … WHERE refunded_amount + x <= amount` + CHECK; invalid amounts (0, negative, text, NaN, Infinity) rejected |
| Money is Decimal end to end | 3 × 0.10 = 0.30 exactly (tested) |

Consequence: a cancelled order can never hold money, so the report needs no
special "paid then cancelled" accounting.

### Automatic table release

When a dine-in order becomes `completed` or `cancelled` and **no other
active order remains on that table**, the table moves `OCCUPIED → CLEANING`
in the same transaction as the order change. A table that staff had already
moved away from OCCUPIED is left alone. Staff still mark it AVAILABLE after
cleaning (M10 known limitation closed). The order-status audit row covers
this; the automatic table change has no separate audit event.

### Routes

| Route | Role |
| --- | --- |
| `POST /staff/orders/<id>/payment` (`method`) | STAFF, ADMIN |
| `POST /staff/orders/<id>/refund` (`amount`, `reason`) | **ADMIN** |

Staff order page: payment card (collect buttons per method, or paid details
+ admin refund form). Queue: "Paid" column (eager-loaded, no N+1). Customer
order page: Paid badge, method, time, refunded amount. Audit:
`payment_recorded`, `payment_refunded`.

### Report definitions (changed)

| Figure | Before (M10) | Now (M11) |
| --- | --- | --- |
| Refunds | 0.00 "not recorded" | Σ `refunded_amount` on completed orders |
| Net sales | gross − 0 − 0 | gross − discounts − refunds |
| Paid orders | = completed orders | completed orders **with a payment** |
| Collected | Σ `total` of completed | Σ (`amount − refunded_amount`) on completed orders |
| Paid in advance | — | net payments on orders not completed yet |
| Completed but unpaid | — | completed orders with no payment (money owed) |
| Discounts, tax | 0.00 "not recorded" | unchanged — still not recorded |

`tests/test_sales.py::test_cancelled_and_unfinished_orders_are_not_sales`
was updated for the new "collected" definition (it now records a payment on
one of the two completed orders and asserts collected = that payment, plus 1
unpaid). `test_completed_order_keeps_its_table_after_table_is_freed` now
asserts the automatic CLEANING instead of setting it by hand. Both are the
requested behaviour changes, not weakened assertions.

### Migration

**`c3a9d7e21f58`** (`down_revision = b5e2f8c41a07`): creates `payments`,
widens `ck_audit_logs_event_type` by 2 events. Downgrade: deletes the 2 new
audit event rows, restores the constraint, `drop_table('payments')` only
(MySQL-safe — no index dropped before its foreign key; see #32). No existing
row touched. Schema-match and round-trip tests pass on SQLite and MySQL.
**Applied to the live `quickjunctiondb` and verified** — see "M11 live MySQL verification" below.

**Results:** SQLite **608 passed, 5 skipped** (the MySQL-only races); MySQL
test database **612 passed, 1 skipped** (#32), 0 failed.

### Tests — 20 + 3 new

- `tests/test_payments.py` (20): server-side amount (forged amount ignored),
  audit, invalid method, double payment (service + DB UNIQUE), DB refund
  CHECK, cancelled order unpayable, RBAC (401/403) + CSRF, partial→full
  refund bounds, 6 invalid refund amounts, stale concurrent refund cannot
  overdraw, refund ADMIN-only + audit, paid order not cancellable by customer
  or staff until fully refunded (partial is not enough), stale cancel after
  payment refused, report figures from payments (refunds, net, paid,
  collected, prepaid, unpaid, AOV), table released only after the last
  active order, release never overrides a non-occupied table, Decimal.
- `tests/test_mysql_concurrency.py` (+3, MySQL only): two cashiers → exactly
  one payment; payment vs cancel → never both (the order is either cancelled
  and unpaid or paid and not cancelled); two full refunds → exactly one.
  15 rounds each, stable over 5 runs.

**Non-vacuity:** removing the "no unrefunded payment" clause from the cancel
UPDATE fails both paid-cancel tests on SQLite and the payment-vs-cancel race
on MySQL (`['cancelled', 'paid']`). Restored.

### M11 live MySQL verification (2026-09-29) ✅ production-verified

Against the live **`quickjunctiondb`** (MySQL 8.0.46), commit `b3b6e89`.

**Backup.** `mysqldump --single-transaction` → `quickjunctiondb_pre_m11.sql`
(exit 0, 27,418 bytes, dump-completed footer present, 12 tables + data; kept
outside the repo; the M10 backup untouched).

**Migration.** Live DB was at `b5e2f8c41a07`. `flask db upgrade` applied
`c3a9d7e21f58` with no error. `flask db current` → **`c3a9d7e21f58
(head)`**, equal to the repository head. `flask db check` shows only the
documented enum-CHECK autogenerate false positive (now also listing the new
`ck_payments_method`); no table/column/index/FK drift.

**Schema (`information_schema`).** `payments` InnoDB; columns and types as
modelled (`decimal(10,2)` money, `refunded_amount` default 0.00,
`captured_at`/`updated_at` default now()); PK; UNIQUE `order_id`; FKs
`order_id → orders` and `recorded_by_id → users`, both RESTRICT; CHECKs
`ck_payments_amount_positive`, `ck_payments_refund_within_amount`,
`ck_payments_method`. `orders` unchanged by M11 (its M10 CHECKs intact).
Audit CHECK: all **26** `AuditEvent` values accepted by direct insert and an
unknown value rejected (3819) — in a rolled-back transaction.

**Existing data survived.** Before and after: users 5, orders 2, order_items
2, menu_items 18, audit_logs 17 (max id 17), payments 0; both orders
unchanged (`pending`, `online`, ₹229.00).

**Behaviour on the live DB — 44/44 checks, zero residue.** Run through the
real routes and services on one connection inside an outer transaction
rolled back at the end (the scoped session was swapped, for that process
only, for a plain `Session` with `join_transaction_mode="create_savepoint"`,
so every service commit released a savepoint). Covered:

- payments: staff record **cash, card, wallet**; amount = server total, a
  forged `amount`/`total` of 1.00 ignored (₹498.00 recorded); duplicate
  refused; customer 403; cancelled order cannot be paid;
- refunds: admin partial (1.00 + reason) and full; over-refund refused both
  after full and as a single oversized request; DB CHECK blocks overdraw
  (3819); a stale second full refund refused; **staff 403, customer 403**;
- cancellation: unpaid pending order cancels; paid order refused for customer
  (409) and staff; partially refunded refused; fully refunded cancels (actor
  `staff`); a stale cancel that never saw the payment refused by the guard
  inside the UPDATE;
- tables: dine-in occupies; completing A leaves the table OCCUPIED while B is
  active; cancelling B → **CLEANING**; historical `table_id` kept; a table
  staff had moved to AVAILABLE is not forced to CLEANING;
- report (today): completed 2, gross 996.00, **refunds 5.00, net 991.00**,
  paid 1, collected 493.00, completed-but-unpaid 1, paid-in-advance 995.00,
  cancelled 3 (never sales), **tax 0.00 / discounts 0.00 "not recorded"**,
  by source dine-in 2, by table T1 493.00 / T2 498.00; admin page renders;
  exactly 4 `payment_recorded` + 4 `payment_refunded` audit rows written.

After rollback every live row count equalled the baseline and no
verification user remained. Two side effects outside the tables, both
harmless: the app's file logger wrote 35 lines for the throwaway users
(ids 6–8) to the git-ignored `logs/security.log`; and InnoDB does not roll
back AUTO_INCREMENT, so the next ids skip ahead (users 12, orders 21,
payments 9, audit 98) — nothing is reused.

**Concurrency** (needs several committing connections, so run on the
disposable `quickjunction_test` on the same server, via the suite): two
cashiers → one payment; payment vs cancel → never both; two full refunds →
one — 15 rounds each, all passing.

**Full suites.** SQLite **608 passed, 5 skipped** (MySQL-only races), 24
warnings, 128 s. MySQL `quickjunction_test` **612 passed, 1 skipped** (#32),
18 warnings, 439 s (both run concurrently, hence the durations). Counts equal
the M11 baseline; 0 failures. Warnings are the pre-existing `env.py`
deprecation (#13).

**Security (read-only review).** Payment route takes only `method` from the
client; order from the URL id + DB, amount from the locked order row. Refund
route is `require_role(ADMIN)` and acts on `order.payment`, never a
client-supplied payment id; amount parsed to `Decimal` and bounded by the
conditional UPDATE + CHECK. Duplicate payment: UNIQUE `order_id`. Races:
row lock + in-UPDATE guards. No raw/f-string SQL, no `|safe`, no debug
output, no secrets in the repo.

**Verdict: M11 is production-verified on live MySQL.** Remaining: tax and
discounts not recorded (#31); full downgrade-to-base on MySQL (#32).

---

## Milestone 12 — Tax + discounts ✅

First milestone of the fast-track plan (M12 tax/discounts → M13 inventory →
M14 kitchen → M15 UI → M16 final audit). Baseline: SQLite **608 passed, 5
skipped**; MySQL test DB **612 passed, 1 skipped**.

### Pricing formula (`app/services/pricing.py` — the only place it lives)

```
discounted_subtotal = subtotal - discount_amount
tax_amount          = discounted_subtotal x tax_rate / 100   (ROUND_HALF_UP to 0.01)
total               = discounted_subtotal + tax_amount
```

Decimal end to end. Percentage discounts: `subtotal x pct / 100`, half-up.
A discount can never exceed the subtotal.

### Configuration

`PricingSettings` (`pricing_settings`, one row, id 1), seeded by the
migration with **tax 13.00 %** and **staff maximum discount 20.00 %** — the
application's configured defaults, not a legal claim. Defaults live once, in
`app/models/pricing_settings.py`; no `0.13` anywhere else. DB CHECKs: tax
0–50 %, staff cap 0–100 %. Admin edits them at **`/admin/settings`**
(`require_role(ADMIN)`, CSRF, validated server-side, audited as
`pricing_settings_changed` with old → new).

### Order snapshot (never recomputed)

New `orders` columns: `discount_type` (`percent`/`fixed`), `discount_value`,
`discount_amount`, `discount_reason`, `discounted_by_id`, `tax_rate`,
`tax_amount`. Checkout snapshots today's tax rate with zero discount;
`total` now equals the formula above. A later discount re-prices using the
**order's own `tax_rate`**, never the current setting (tested by changing the
rate to 5 % and discounting an old 13 % order). Menu price changes never
reach an order. Existing orders were backfilled with discount 0 and tax 0 —
exactly what they were charged.

DB CHECKs: `discount_amount` between 0 and `subtotal`; `tax_amount >= 0`;
`ck_orders_total_formula`: `ABS(total - (subtotal - discount_amount +
tax_amount)) < 0.005` — a half-cent tolerance because SQLite stores NUMERIC
as binary float (on MySQL DECIMAL it is exact; any real error is ≥ 1 cent).

### Discounts — who and when

| Actor | May discount |
| --- | --- |
| Customer | never (no route; checkout ignores posted discount/tax/total fields — tested) |
| STAFF | up to `staff_max_discount` % of the subtotal; a fixed amount is measured as a % too |
| ADMIN | up to 100 % |

Every non-zero discount needs a reason (≤ 255 chars); value 0 removes the
discount. Only **unpaid** orders that are not completed/cancelled can be
discounted. Route: `POST /staff/orders/<id>/discount` (STAFF, ADMIN; CSRF;
only type/value/reason are read from the form — forged `discount_amount`,
`tax_amount`, `total`, `discounted_by_id` are ignored, tested). Audited as
`order_discount_applied` with type, value, amount and new total.

### Payments, refunds, cancellation

Unchanged code paths: payment still takes the server-side `order.total`,
which now includes discount and tax. Refunds remain bounded by the actual
payment. Paid-order cancellation rules unchanged.

**Real bug found by the MySQL race test and fixed.** `apply_discount` locked
the order row but checked "already paid?" via `locked.payment` — a lazy load,
which under MySQL REPEATABLE READ is a *snapshot* read and could miss a
payment committed after the transaction's first read. The race test caught
it immediately (payment 226.00 recorded, then a discount moved the total to
203.40). Fixed with a locking `SELECT … FOR UPDATE` on `payments`. SQLite
could never show this. (`record_payment`'s similar `locked.payment` read is
already backstopped by the UNIQUE `order_id`; the cancel guard is a
`NOT EXISTS` inside the UPDATE, a locking read — neither is exposed.)

### Reporting

Reports now read the snapshots: **Discounts** = Σ `discount_amount`, **Tax**
= Σ `tax_amount` (each order's own rate), **Net sales** = gross − discounts −
refunds (tax excluded — collected for the tax authority). The "not recorded"
labels are gone. Changing settings after the fact does not move any reported
figure (tested).

### UI

Staff order page: subtotal / discount (with reason) / tax (rate) / total and a
discount form (unpaid, unfinished orders only; shows the role's cap).
Customer order page: the same breakdown. Checkout: tax preview at the current
rate (display only — recomputed server-side). Admin nav: **Settings**.

### Migration

**`d7f1e3a9c2b4`** (`down_revision = c3a9d7e21f58`): creates and seeds
`pricing_settings`; adds the 7 order columns, FK `discounted_by_id → users`
(RESTRICT) and 4 CHECKs; widens the audit allow-list by 2 events. Downgrade
drops the FK before the columns (MySQL-safe, see #32). Schema-match and
round-trip tests pass on SQLite and MySQL. **Not applied to live
`quickjunctiondb` yet (#34).**

### Tests

- `tests/test_pricing.py` (**32**): default snapshot; percentage, fixed and
  zero discounts; half-up rounding (0.065 → 0.07, 4.9995 → 5.00); staff cap
  exactly / just over (percent and fixed); admin 100 %; 6 invalid/excessive
  values; reason required and bounded; DB CHECKs; settings changes never
  touch past orders and re-pricing uses the order's own rate; menu repricing;
  5 invalid settings; settings page 401/403/403/admin + audit; discount route
  401/403 + forged fields ignored + audit; customer cannot self-discount at
  checkout; CSRF; payment takes the discounted taxed total and locks pricing;
  stale discount after payment refused; completed/cancelled not
  discountable; refunds bounded by the payment; report uses snapshots after
  settings change; order and checkout pages show the breakdown.
- `tests/test_mysql_concurrency.py` (+1, MySQL only): discount vs payment —
  the payment always equals the order's final total; stable over 5 runs
  after the fix (failed every run before it).
- **Existing tests updated** (behaviour change, not weakening): 13 assertions
  that hard-coded "total == subtotal" (no tax existed) now assert the exact
  taxed values, e.g. 59.97 → tax 7.80 → 67.77, 498.00 → 562.74, and one M11
  report assertion now expects tax 325.00 instead of 0.00.
  `test_17_client_total_manipulation_ignored` was strengthened to also forge
  `tax_amount` and `discount_amount`.

**Non-vacuity:** removing the paid-order check and the staff cap from
`apply_discount` fails exactly the 3 matching tests; restored.

**Results:** SQLite **640 passed, 6 skipped** (608 + 32; +1 MySQL-only
skip), 24 warnings. MySQL test DB **645 passed, 1 skipped** (#32), 18
warnings, 417 s. 0 failures.

### Security review

New routes: discount `require_role(STAFF, ADMIN)`, settings
`require_role(ADMIN)`; both CSRF-protected. Only type/value/reason (and the
two settings values) come from the client; every amount is computed
server-side from the locked order row. Caps enforced from the actor's
server-side role. Row lock + locking payment read close the discount/payment
race. No raw/f-string SQL, no `|safe`, no debug output, no secrets.

---

## Milestone 13 — Inventory / stock management ✅

Baseline: SQLite **640 passed, 6 skipped**; MySQL test DB **645 passed, 1
skipped**. A practical restaurant inventory, not an ERP.

### Architecture — extends, does not duplicate

The M03 `Ingredient` and `MenuItemIngredient` tables were **extended** rather
than a parallel inventory model added:

- **`Ingredient`** + `unit` (`g`/`kg`/`ml`/`l`/`piece`), `current_quantity`,
  `minimum_quantity` (CHECK ≥ 0), `is_active` ("tracked in inventory"),
  `updated_at`. Quantities `Numeric(12,3)`.
- **`MenuItemIngredient.quantity`** = the **recipe** amount per portion.
  0 (server default) means "listed for recommendations, not stock-tracked"
  — every link the menu form's comma list creates — so the menu form and
  recommendations are unchanged, and editing a menu item's ingredient list
  never erases a recipe amount (SQLAlchemy only inserts/deletes changed
  association rows; tested).
- **`StockMovement`** (`stock_movements`, new): the ledger — `movement_type`
  (`purchase`, `restock`, `sale`, `waste`, `adjustment`), signed
  `quantity_change`, `quantity_after`, `order_id` (sales only; CHECK),
  `actor_id`, `note`, `created_at`.

### Rules (`app/services/inventory.py`)

- **Quantity never changes without a ledger row.** One private writer,
  `_move`, applies `UPDATE … SET current_quantity = current_quantity +
  :delta` (atomic under concurrency) and records the resulting level in the
  same transaction. Creating/editing an ingredient never touches quantity;
  opening stock is a PURCHASE.
- **Sale deduction** happens inside the existing atomic `→ COMPLETED`
  transition, in the same transaction (a failure rolls back both — tested
  by injecting a failure after the deduction). Quantity = Σ portions ×
  recipe amount over tracked recipe lines. **Idempotent twice over**: the
  function skips ingredients already deducted for that order, and UNIQUE
  `(order_id, ingredient_id)` rejects a second SALE row at the database.
  Cancelled orders never deduct; a completed order cannot be cancelled.
- **Insufficient stock** is refused **at checkout** ("Sorry, we are out of
  bun, patty…"). Completion is never blocked — a served order must be able
  to close — so concurrent orders can take stock negative; it shows as
  **short** (#35).
- **Low stock**: tracked and `current ≤ minimum` (with a minimum set) or
  negative.
- WASTE and ADJUSTMENT require a note. Quantities are Decimal, finite,
  bounded (≤ 1,000,000), non-zero; WASTE is entered positive and stored
  negative; ADJUSTMENT is signed.

### Authorization

| Action | Customer | STAFF | ADMIN |
| --- | --- | --- | --- |
| View stock, history, usage | ✗ 403 | ✓ | ✓ |
| Record purchase / restock / waste | ✗ | ✓ | ✓ |
| Record adjustment (count correction) | ✗ | ✗ (route + service) | ✓ |
| Create/edit ingredients, recipes | ✗ | ✗ 403 | ✓ |

Every manual change has an actor (the movement row) and an audit event;
sales carry the completing staff member and the order id.

### Routes / UI

| Route | Role |
| --- | --- |
| `GET /staff/inventory?days=7\|30\|90` — stock list, status (ok / low / short / not tracked), low-stock banner, usage / wastage / purchases+restocks in the window | STAFF, ADMIN |
| `GET /staff/inventory/<id>` — movement history (ledger with running level, actor, note) + record form | STAFF, ADMIN |
| `POST /staff/inventory/<id>/movement` | STAFF, ADMIN (adjustment ADMIN only) |
| `GET/POST /admin/inventory/new`, `/admin/inventory/<id>/edit` | ADMIN |
| `GET/POST /admin/menu/<item_id>/recipe` — per-portion amounts, add an ingredient | ADMIN |

Nav: **Inventory** for staff/admin; **Recipe** button on the admin menu list.
Reports show quantities only — no cost is stored, so no valuation is claimed
(#36).

Audit events added: `stock_item_saved`, `stock_movement_recorded`,
`recipe_updated` (31 total).

### Migration

**`e5b8c1d4f7a2`** (`down_revision = d7f1e3a9c2b4`): 5 ingredient columns +
2 CHECKs; `menu_item_ingredients.quantity` + CHECK; `stock_movements` with 3
FKs (RESTRICT), 3 CHECKs, the unique idempotency key and
`ix_stock_movements_ingredient_created`; audit allow-list +3. Existing
ingredients start at 0 and existing links at recipe 0 — no behaviour change
until an admin enters stock and recipes. MySQL-safe downgrade (drop_table /
column drops only). **Not applied to live `quickjunctiondb` yet (#34).**

### Tests

- `tests/test_inventory.py` (**25**): ingredient creation + validation (no
  movement from a definition); purchase / restock / waste / adjustment
  ledgered with correct running levels and actors; 9 invalid movements
  (zero, negative, text, NaN, oversize, missing note for waste/adjustment,
  manual "sale", unknown type); staff cannot adjust; recipe amounts survive
  the menu form; exact deduction on completion, none before it; retried
  processing deducts nothing; DB rejects a second SALE row; cancelled /
  unfinished orders deduct nothing; untracked ingredients skipped; failed
  completion rolls back the deduction; checkout refuses what cannot be made
  (service + customer-facing message); served order completes even when
  stock goes short; low-stock detection; RBAC (customer 403 ×5, staff view +
  purchase allowed, admin pages 403, staff adjustment refused) + audit;
  admin ingredient + recipe management + audit + low banner; CSRF.
- `tests/test_mysql_concurrency.py` (+1, MySQL only): two order completions
  and a purchase hit one ingredient simultaneously, 15 rounds — the level is
  exact every round and always equals the ledger sum; stable over 3 runs.

**Non-vacuity:** removing the idempotency skip → the DB unique key still
refuses the duplicate (test fails loudly); removing the deduction call → 6
tests fail; removing the checkout stock check → 2 fail. All restored.

**Results:** SQLite **665 passed, 7 skipped** (640 + 25; +1 MySQL-only
skip), 24 warnings. MySQL test DB **671 passed, 1 skipped** (#32), 18
warnings, 736 s (run overlapped the SQLite run). 0 failures. No existing test
needed changing (only the pinned table list gained `stock_movements`).

### Security review

All six routes `require_role`-gated as tabled; POSTs CSRF-protected; the
service re-checks the ADJUSTMENT role; recipe form reads only `q_<int>` keys
and refuses ingredients not linked to the item; all SQL via ORM/bound
parameters; no `|safe`; no secrets or debug output. Customers see only an
"out of …" message at checkout — never stock levels.

---

## Milestone 14 — Kitchen operations ✅

Baseline: SQLite **665 passed, 7 skipped**; MySQL test DB **671 passed, 1
skipped**. A focused kitchen screen — no new statuses, no new write path.

### Design

- **Queue** (`app/services/kitchen.py::queue`): orders in PENDING /
  CONFIRMED / PREPARING / READY, grouped into four columns, **oldest first**.
  SERVED / COMPLETED / CANCELLED leave the board. Items and table are
  eager-loaded — a flat 3 queries however long the queue (tested).
- **Actions**: one forward step per column (confirm → start preparing → mark
  ready). Each button posts to the **existing** `POST
  /staff/orders/<id>/status` route, so CSRF, the transition allow-list, the
  atomic conditional UPDATE (M10), the paid/stock rules (M11/M13) and the
  `order_status_changed` audit all apply unchanged. Serving/completing and
  cancelling stay on the full staff order page. The route gained
  `return_to=kitchen` — an **allow-list of one named page**, never a URL, so
  it cannot become an open redirect (tested with four hostile values).
- **Timestamps**: new nullable `orders.preparing_at` / `orders.ready_at`,
  set by the database clock **inside the same conditional UPDATE** that
  moves the order to PREPARING / READY — so only the winning transition
  stamps them, and later steps never rewrite them. Existing orders keep NULL
  (their timing was never recorded).
- **Card**: order number, age (red after 30 min), source and table, cooking
  / ready time, each item with quantity. **No customer data** — no username,
  email or anything else (tested). Notes: orders have no notes field, so
  none are shown (#37).
- **Stats** (header): prepared today (count of `ready_at` today), average
  preparation time (`ready_at − preparing_at`, today), longest-waiting active
  order. Deliberately minimal.
- Wall-screen friendly: `<meta http-equiv="refresh" content="30">` via a new
  `head` block in `base.html`; no JavaScript.

| Route | Role |
| --- | --- |
| `GET /staff/kitchen` | STAFF, ADMIN (customer 403, anonymous 401) |

Nav: **Kitchen** for staff/admin.

### Concurrency

Unchanged mechanism, now also covering the stamps: the MySQL cancel-vs-
start-preparing race test (15 rounds) was **strengthened** to assert that a
cancelled order has no `preparing_at` and a preparing one has it — stable
over 3 runs. A SQLite stale-view test shows a losing kitchen tap leaves no
stamp.

### Migration

**`f3a6d9b2e8c5`** (`down_revision = e5b8c1d4f7a2`): two nullable DateTime
columns on `orders`; no audit change. **Not applied to live
`quickjunctiondb` yet (#34).**

### Tests

`tests/test_kitchen.py` (**14**): queue grouping, oldest-first, finished
orders hidden; query count flat; stamps set on PREPARING / READY and never
rewritten; a losing (stale) transition stamps nothing; average prep time,
prepared-today (yesterday excluded) and longest-waiting; empty stats; RBAC
(401 / 403 / staff 200 / admin 200); board shows items, quantities,
table/source, refresh — no customer name or email; kitchen buttons drive
the real workflow, audit, and return to the board; `return_to` allow-list
vs four hostile values; skipping a step is rejected and audited; customer
403 on kitchen actions; CSRF; cancelled orders leave the board.

**Non-vacuity:** removing the `preparing_at` stamp fails the timestamp test;
removing the status filter from the queue fails 2 tests. Restored.

**Results:** SQLite **679 passed, 7 skipped** (665 + 14), 24 warnings.
MySQL test DB **685 passed, 1 skipped** (#32), 18 warnings, 341 s. 0
failures. No existing assertion was weakened (one MySQL race test gained two
assertions).

### Security review

Kitchen route `require_role(STAFF, ADMIN)`; all state changes go through the
existing CSRF-protected, role-gated, allow-listed, atomic status route;
`return_to` is a fixed allow-list; no customer PII in the view; no `|safe`;
no new SQL outside the ORM.

---

## Milestone 15 — UI/UX overhaul ✅

Presentation layer only: **no backend logic, business rule, route guard or
schema changed** (no migration). Same stack — Flask templates, vendored
Bootstrap 5.3.3, one small stylesheet, no new JavaScript or dependency.
Baseline: SQLite **679 passed, 7 skipped**; MySQL test DB **685 passed, 1
skipped**.

### Design system (`app/static/css/app.css`)

- **Brand**: one terracotta accent (`#b4462b`, 5.4:1 on white — AA for
  text) wired into Bootstrap's own variables (`--bs-primary`, link colour,
  focus ring, `.btn-primary` / `.btn-outline-primary`), so existing templates
  follow it without edits. Dark ink navbar.
- **Components**: page header (`.qj-page-header`, `.qj-eyebrow`), KPI tile
  (`.qj-kpi` + accent/warn variants), section headers, table header styling
  and hover, rounded cards, customer welcome band (`.qj-hero`). All earlier
  classes kept.
- **Status badges** — one partial per vocabulary, each status with its own
  colour pair from Bootstrap 5.3's AA-contrast "subtle" palette, and the
  status **text** always rendered (colour is never the only signal):
  `_status_badge.html` (7 order statuses — SERVED now distinct from READY,
  uniqueness tested), `_table_status_badge.html`, new
  `_reservation_badge.html` (replacing three inline copies).
- Fixed a pre-existing mojibake in the stylesheet header.

### Navigation (`base.html`)

Role-based, and a display convenience only — every route still enforces
its role server-side (tested per role, including direct GETs):

| Role | Links |
| --- | --- |
| Anonymous | Menu · Cart · Sign in · Register |
| Customer | Dashboard · Menu · For you · My orders · Reservations · Cart · profile |
| Staff | Dashboard · Orders · Kitchen · Tables · Reservations · Inventory · profile |
| Admin | staff links + **Manage** ▾ (Sales reports · Payments & refunds · Menu & recipes · Users (staff accounts) · Settings · Audit log) |

Staff/admin no longer see the customer-only Cart / "My orders" / "For you".
The current section is marked `aria-current="page"` (sub-pages highlight
their section). Added a skip-to-content link and a `main` landmark; focus
rings extended to form controls.

### Dashboards (`/account/`, one per role)

Built by the new **`app/services/dashboard.py`** — composition only, every
figure from an existing service (sales, kitchen, tables, reservations,
inventory, orders, recommendations); no new query logic or money
arithmetic. Data scope follows RBAC:

- **Customer**: welcome band with quick actions, *current order* card,
  recent orders (with "can cancel"), top-3 "Picked for you" from the
  existing recommendation engine, upcoming reservations, tables free now.
  Own data only.
- **Staff**: kitchen counts by column, table status counts, today's
  reservations, low-stock list, today's order *counts*. **No money** —
  sales stay ADMIN-only; the dashboard does not widen that (asserted: no
  currency symbol on the staff dashboard).
- **Admin**: the staff view plus today's net sales, month net sales,
  collected today with paid/unpaid split, refunds this month, orders /
  cancelled / paid-in-advance today, pending staff accounts, and quick
  actions. KPIs link to their detail pages.

The old single `account.html` was replaced by `dashboard/customer.html`,
`dashboard/staff.html` and `dashboard/admin.html` (admin extends staff).

### New read-only admin views (`app/routes/admin_views.py`, ADMIN only, GET only)

- **`/admin/payments`** — latest 200 payments: order, table, time, method,
  amount, refunded (+ reason), recorded by, order status; totals taken /
  refunded. Refunds are still made on the order page (M11 route).
- **`/admin/audit`** — latest 200 audit events, filterable by event type via
  an **allow-list** (an unknown/hostile value falls back to "all" and never
  reaches SQL); metadata rendered as escaped text (XSS payload tested).

### Tests — 22 new (`tests/test_ui_overhaul.py`)

Anonymous / customer / staff / admin navigation shows exactly the permitted
links; staff are also refused the admin URLs directly (the menu list stays
viewable by staff, as since M03); `aria-current`; skip link + landmarks;
404 page renders the layout; customer dashboard own-data-only; staff
dashboard operational and money-free; admin dashboard sales figures (net
100.00 vs collected 113.00 with tax); payments page RBAC + refund display;
audit page RBAC, allow-list filter, hostile filter value, escaping; every
order status has a unique badge (7 cases); **crawl** — every link each role
is shown returns 200 (3 cases). No existing test changed: the pinned
strings ("Order queue", "2 pending", "Book a table", "Tables free right
now", "Upcoming reservations", no staff links for customers, no
recommendation links for staff/admin) were designed around.

**Results:** SQLite **701 passed, 7 skipped** (679 + 22), 24 warnings.
MySQL test DB **707 passed, 1 skipped** (#32), 18 warnings, 363 s. 0
failures. The existing template scan (`|safe` / autoescape) covers every new
template.

### Not done here

A visual review in a real browser (#38) — the rendering tests assert
content and RBAC, not appearance. Planned for the M16 demo audit.

---

## Milestone 16 — Final integration, security and demo audit ✅

No new features. Baseline: SQLite **701 passed, 7 skipped**; MySQL test DB
**707 passed, 1 skipped**.

### Live database brought current (closes #34)

`mysqldump --single-transaction` → `quickjunctiondb_pre_m16.sql` (exit 0,
29,074 bytes, completed, 13 tables; M10/M11 backups kept). `flask db
upgrade` applied `d7f1e3a9c2b4` → `e5b8c1d4f7a2` → `f3a6d9b2e8c5` with no
error; `flask db current` = **`f3a6d9b2e8c5 (head)`** = repository head.
`flask db check`: only the documented enum-CHECK false positive. Verified:
row counts unchanged (5 users, 2 orders, 18 menu items, 36 ingredients, 51
links, 17 audit rows); `pricing_settings` seeded 13.00 / 20.00; both old
orders backfilled discount 0 / tax 0 (formula CHECK holds); ingredients at 0
stock, recipe links at 0; every new CHECK and the stock idempotency key
present; all 31 audit events accepted (rolled-back probe).

### Data-integrity audit — live DB, 10 checks, 0 violations

Order total = subtotal − discount + tax; subtotal = Σ line totals; line =
unit snapshot × qty; payment = order total; no cancelled order holds
unrefunded money; stock = ledger sum; sale movements only on completed
orders; reservation slot flag ↔ status; dine-in ↔ table; cancellation
records complete. The same checks close the new end-to-end demo test.

### Security audit and fixes

- **Security headers (closes #7)** — `app/utils/headers.py`: CSP
  `default-src 'self'`, **`script-src 'self'` with no `'unsafe-inline'`**,
  `object-src 'none'`, `base-uri`/`form-action 'self'`,
  `frame-ancestors 'none'`; `X-Frame-Options: DENY`; `nosniff`;
  `Referrer-Policy: same-origin`; `Permissions-Policy`; HSTS only when cookies
  are Secure (production). To make the strict script policy possible, the
  only two inline handlers (submit-once button, inventory window select) were
  moved into `static/js/loading.js` as `data-submit-once` / `data-autosubmit`
  — progressive enhancement, no behaviour lost. Inline `style=""` stays
  allowed (styles cannot run code).
- **No caching of signed-in pages (closes #25)** — `Cache-Control: no-store`
  + `Vary: Cookie` on every page served to a signed-in user; public pages and
  static assets unaffected.
- **Whole-app route sweep** — `tests/test_final_audit.py` enumerates all 78
  route/method pairs from the URL map: anonymous callers reach only an
  explicit public allow-list (home, menu, cart, health, login, register);
  the 18 admin-only pairs refuse customers and staff; the 14 staff pairs
  refuse customers. Proven non-vacuous: removing the guard from
  `/admin/payments` and `/staff/kitchen` failed 4 tests; restored.
- Also asserted: headers on every response including 404s; no inline script
  in any template (so the CSP can never silently break a page); DEBUG off in
  testing/production.
- Reviewed and found sound (no change needed): Argon2id hashing, session
  revocation (M09), CSRF app-wide, ownership-in-query IDOR protection (404),
  server-side money everywhere, atomic/locked transitions for every race
  (orders, payments, refunds, discounts, stock, reservations), audit writer
  refusing secret keys, no `|safe`, ORM/bound SQL only.

### Performance

Query counts measured per page at 5 and 30 orders: **every page flat** (no
N+1) — dashboards 15, reports 7, staff orders 5, kitchen 5, payments 4,
tables/inventory 3, reservations 2, customer orders 1. One real waste fixed:
the admin dashboard built today's sales report twice; it now builds it once.

### Browser walkthrough (closes #38)

Run on a scratch SQLite DB (migrated from empty through all 11 revisions,
seeded with `scripts/seed_demo.py` + demo orders/stock) — never the live DB.
In Chrome: **customer** dashboard (hero, current order, status badges,
reservations, free tables) and a discounted order (349.00 − 34.90 + 40.83
tax = 354.93, paid); **staff** dashboard (kitchen counts, table statuses, low
stock, no money) and the kitchen screen, including a real "Start preparing"
that moved the card and returned to the board; **admin** dashboard (net
458.00 vs collected 517.54 with tax, paid in advance 354.93), sales report
and audit log (which faithfully recorded the session, including a refused
customer login on the admin portal). Console: **no CSP violations or JS
errors**.

### Tests — 9 new

- `tests/test_final_audit.py` (8): anonymous sweep, admin-only sweep ×2
  roles, staff sweep, security headers, no-store caching, no inline script,
  debug off.
- `tests/test_demo_flow.py` (1): the complete demo through real HTTP —
  admin settings/tables/stock/recipe → staff sign-up + approval → customer
  registers, browses, dine-in order (13 % tax) and cancels (table
  auto-released) → takeaway order → reservation (rival customer gets 404 on
  both) → kitchen (no customer name) → staff discount over the admin-set 15 %
  cap refused, 10 % applied → confirm / prepare / ready → customer cancel
  refused → card payment at the server total → completion deducts stock →
  staff refund refused, admin refund → report figures (gross, discounts, tax,
  refunds, net, collected, cancelled) → payments and audit pages → menu
  repricing and a tax change leave the order untouched → all integrity checks.

**Results:** SQLite **710 passed, 7 skipped** (701 + 9), 24 warnings. MySQL
test DB **716 passed, 1 skipped** (#32), 18 warnings, 348 s. 0 failures. No
existing test changed.

---

## Final state (end of M16)

### Milestones

M01–M09 foundation → auth/RBAC/audit → menu → cart/orders → staff workflow →
preferences + recommendations → local LLM explanations → UI polish → session
revocation; M07.x AI dataset/adapter iterations (V4 in production behind a
deterministic guard); fast-track M10 restaurant operations, cancellation,
tables, reservations, sales → M11 payments/refunds → M12 tax/discounts → M13
inventory → M14 kitchen → M15 UI overhaul → **M16 final audit**.

| Item | Value |
| --- | --- |
| Migration head | **`f3a6d9b2e8c5`** — repository and live `quickjunctiondb` |
| Tests | SQLite **710 passed, 7 skipped (MySQL-only races)**; MySQL test DB **716 passed, 1 skipped (#32)**; 0 failing |
| Database | MySQL 8.0.46 (live verified at every schema milestone); SQLite for tests |
| Security | Argon2id, server-side session revocation, CSRF, RBAC on every route (swept), ownership-in-query IDOR, CSP + anti-framing + no-store, audit log with secret-key refusal |
| Data integrity | DB CHECK/UNIQUE constraints for money formulas, statuses, stock ledger idempotency, reservation slots; 10 integrity checks pass on live data |

### Architecture (one line per layer)

Flask app factory + blueprints (routes parse/authorize) → services (all
business rules, money arithmetic in `pricing`/`payments`, atomic conditional
UPDATEs and row locks for every race) → SQLAlchemy models with CHECK/UNIQUE
backstops → MySQL; Jinja templates on vendored Bootstrap with one small
design layer; reporting and dashboards are pure reads composed from the
services — no duplicated sales or order data anywhere.

### Demo flow (≈ 10 minutes, `scripts/seed_demo.py` accounts, password `demo-password-1`)

1. **admin** → Dashboard (sales KPIs) → Manage ▸ Settings (tax 13 %, staff
   cap) → Menu & recipes ▸ *Recipe* on a dish → Inventory ▸ record a purchase.
2. **customer** → Menu → add to cart → Checkout (tax preview) → dine-in at a
   table → Dashboard shows the current order → cancel it (table goes to
   cleaning) → order again → Book a table.
3. **staff** → Kitchen → Confirm → Start preparing → Mark ready → order page
   → apply a discount (cap enforced) → record payment → Serve → Complete
   (stock deducted on the Inventory page).
4. **admin** → refund part of it → Sales reports (gross / discounts / tax /
   refunds / net / collected) → Payments & refunds → Audit log (every step
   above is there).

### Remaining known limitations (all documented above)

#3 cart discarded on login · #4 checkout idempotency by cart clearing ·
#5 in-process rate limiter · #8 no audit retention job · #12/#39 unpaginated
lists · #13 `env.py` deprecation warnings · #14–#18 recommendation-engine
limits · #20a/#21–#24/#27 local-LLM limits · #26a logout revokes all devices ·
#28 no production deployment config · #29 older tests not audited for the
identity-cache weakness · #32 full downgrade-to-base fails on MySQL (pre-M10
migrations) · #35 stock checked not reserved · #36 no ingredient costs ·
#37 no kitchen notes. Plus: `.env` connects as MySQL `root` — the app should
use its own least-privilege account (`docs/MYSQL_SETUP_HANDOFF.md` §5).

### Next steps (if the project continues)

Least-privilege MySQL account and a production deployment config (#28);
pagination on the long lists (#12/#39); optional kitchen notes (#37);
cleaning up #32 and #13; ingredient costs if profit reporting is wanted
(#36).
