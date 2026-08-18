# Quick Junction — Project Progress

**Authoritative project-progress document.** Records only work that has
actually been implemented and verified against the repository. If a
feature is not listed here as complete, assume it does not exist.

Last updated: 2026-08-18 (end of Milestone 09)
Current state: **Milestone 09 complete — demo-ready, session replay closed.**

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
| — | Payment gateway, production deployment | ⬜ Not started |

**Test suite: 239 passing, 0 failing** (`pytest`; in-memory SQLite for
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
| 7 | No security headers (HSTS, CSP, `X-Frame-Options`, …) | required before real users |
| 8 | Audit log retention not enforced in code | `audit_logs` holds IPs; needs a purge policy |
| 9 | `tests/__init__.py` exists solely to stop a stray `tests` package in site-packages from shadowing the local one | environment-specific workaround, not a project need |
| 10 | Order status changes have no concurrency guard — two staff moving the same order simultaneously both read the same current status, and the second write wins | rare double-transition under real concurrent kitchen use |
| 11 | `CANCELLED` sets a status and nothing else — no refund, restock, notification, or customer-initiated cancellation | cancellation is not operationally complete |
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
| 25 | No `Cache-Control` headers on authenticated pages — a browser served a stale `/recommendations/explain` during testing | stale personalised content after preference changes |
| 26 | ~~Logout does not revoke a copied session cookie~~ — **closed in M09** via `users.session_version`; verified live on MySQL with controls, 18 regression tests | — |
| 26a | Logout revokes **all** of that user's sessions, not just the current device — a deliberate consequence of using one counter instead of per-session state | logging out on a phone signs the account out on a laptop too |
| 29 | **Test-harness weakness**: the pytest `app` fixture holds one application context open, so `g.current_user` persists across requests and clients within a test. Production is unaffected (one context per request), but identity-switching tests can pass for the wrong reason unless they clear it | older suites not yet audited for this |
| 27 | AI explanation takes ~9 s per request on CPU after the model is loaded (23 s including first load) | acceptable on its own route; would not be acceptable inline |
| 28 | No production deployment configuration (no WSGI service unit, TLS termination, or reverse-proxy config in the repo) | deployment is out of scope so far |

Full ranked pre-production list: `docs/SECURITY.md` §17.

---

## Not implemented — do not assume these exist

Payment gateway / card processing · refunds · customer-initiated
cancellation · **AI chatbot / conversational assistant** (M07 built
explanation-only inference, not a chat surface) · advanced analytics ·
reporting dashboard · deployment configuration · styled frontend
(Bootstrap/JS — every page is plain unstyled HTML) · password reset ·
email verification · staff queue pagination/filtering.

`ai/`, `dataset/`, `training/` are intentionally empty placeholders.

---

## Exact current state (verified at end of M05)

| Item | Value |
| --- | --- |
| Tests | **239 passing, 0 failing, 0 skipped**, 12 warnings (all the pre-existing `env.py` deprecation) |
| Test files | foundation 10, auth 29, menu 26, orders 28, migrations 4, staff orders 20, recommendations 26, llm 32, dataset_v2 22, ui 24, session revocation 18 |
| Migrations | **5 revisions, head = `2d9f3b20045f`** (M09 added `users.session_version`) |
| Tables | 9: `users`, `audit_logs`, `categories`, `menu_items`, `ingredients`, `menu_item_ingredients`, `customer_preferences`, `orders`, `order_items` |
| Models | 10 modules in `app/models/` |
| Services | 10 modules in `app/services/` |
| Blueprints | 10: health, main, auth, account, menu, admin_menu, cart, orders, staff_orders, preferences |
| Audit events | 16 |
| Local LLM | Qwen3-0.6B-Base + **v2** LoRA adapter (20 MB); CPU-only, offline, explanation-only. v1 adapter retained |
| Dataset | **v2: 150** hand-authored examples (120 train / 30 validation), 15 categories. v1 (60) preserved |
| Order statuses | 6 (`pending`, `confirmed`, `preparing`, `ready`, `completed`, `cancelled`) |
| Roles | 3 (`admin`, `staff`, `customer`) |
| Sessions | signed cookie (user id + version); server-side revocation via `users.session_version` |
| Recommendation engine | deterministic TF-IDF + cosine similarity, in-process; **no LLM** |
| Frontend | Bootstrap 5.3.3, vendored locally (no CDN); 1 base layout + 3 partials + 19 pages |
| Database (tests) | in-memory SQLite; migration tests use temporary on-disk SQLite |
| Database (target) | **MySQL 8.0.46 — verified end to end in M08** |
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

## Next milestone

**Train V4 and re-run the M07.3 evaluation.** That is the only remaining step
in this line of work, and the hypothesis is falsifiable:

```
python training/train_lora.py --dataset-version v4 --epochs 3
python training/evaluate_production.py --systems base v2 v3 v4     --json training/eval_results_m07_5.json
```

Requirements before training:

- `v4` must be registered in `training/train_lora.py::DATASET_VERSIONS`
  (currently v1/v2/v3 only, so `--dataset-version v4` will be rejected by
  argparse -- the same blocker M07.4's predecessor hit).
- Expect ~150 optimizer steps (195 examples x 3 epochs / grad-accum 4) and
  roughly 75-80 minutes on CPU, extrapolating from V3's 123 steps in 61.4 min.
- The adapter must go to `models/qwen3-0.6b-quickjunction-lora-v4/`, never over
  V1, V2 or V3. The existing overwrite guard covers this once registered.

Success criterion, decided in advance: **NOT_SET_ERROR drops to 0** while
HALLUCINATION, OVERCLAIM, DIETARY_COMPATIBILITY_ERROR and CROSS_DIMENSION_ERROR
stay at 0 and contrastive tracking stays at 9/9. If not-set clears without
regressions, promoting becomes a supportable decision for the first time.

Two smaller items, unchanged:

- `training/evaluate.py` still contains four scenarios the application cannot
  produce; retiring or re-pointing them is deferred work, not a blocker.
- Known issue #29: auditing the older test suites for the `g.current_user`
  app-context caching weakness found in M09.
