# Quick Junction — Current Status

**This is the authoritative statement of the current state.** It describes the
code that exists today. Milestone history lives in `docs/PROJECT_PROGRESS.md`
(historical log, not a status page).

> **Quick Junction v0.9.0 is an audited prototype baseline. It is NOT approved
> for production deployment.** The go-live gate is
> [`docs/PRODUCTION_READINESS.md`](PRODUCTION_READINESS.md); almost every
> production item there is NOT STARTED.

---

## Baseline record (verified 2026-10-03)

| Item | Value | How it was verified |
| --- | --- | --- |
| Release | **v0.9.0** (annotated tag on the audited commit) | `git tag` |
| Baseline commit | `201f55ae7b4c2b2b20e5fd140387baa6f99bd102` (`201f55a`) | `git rev-parse` |
| Migration head | **`f3a6d9b2e8c5`** — 11 linear revisions, single head | Alembic `ScriptDirectory.get_heads()` |
| Python (production CI baseline) | **3.11** (CI rehearsed in `python:3.11-slim`, 3.11.17) | CI workflow + container run |
| Python (local dev machine) | 3.10.11 | `.venv` |
| Supported Python range | 3.10–3.11 only: `numpy==1.24.3` has no wheels for 3.12+ | `requirements.txt` |
| Tests collected | **725** | pytest |
| SQLite, full repository (`models/` present, Python 3.10) | **718 passed, 7 skipped, 0 failed** — the 7 skips are the MySQL-only concurrency tests | local run |
| SQLite, clean checkout (no `models/`, Python 3.11) — what CI sees | **713 passed, 12 skipped, 0 failed** — 7 MySQL-only + 5 that need the git-ignored `models/` | `python:3.11-slim` container |
| MySQL 8.0 (disposable container), full repository | **724 passed, 1 skipped, 0 failed** — the skip is known issue #32 (full downgrade to base) | local run against `mysql:8.0` (8.0.46) |
| MySQL 8.0, clean checkout (Python 3.11) — what CI sees | **719 passed, 6 skipped, 0 failed** — #32 + 5 `models/` tests | container run |
| MySQL concurrency tests | **7 of 7 executed and passed** (not skipped) | `pytest tests/test_mysql_concurrency.py -v` |
| Runtime dependency audit | `pip-audit -r requirements.txt`: **no known vulnerabilities** | pip-audit 2.10.1 |
| AI-stack dependency audit | `requirements-ai.txt`: **13 known advisories** in torch, transformers, accelerate — see below | pip-audit 2.10.1 |
| Branch protection on `main` | **Not verified / not configured by tooling** — see `docs/RELEASE_PROCESS.md` | no `gh` CLI or API access available |

### Why the 7 MySQL tests skip by default

`tests/test_mysql_concurrency.py` has an autouse fixture that skips unless the
engine dialect is `mysql`. The default test database is in-memory SQLite, which
cannot reproduce InnoDB row locks or concurrent connections, so the races are
only meaningful on MySQL. They run when `TEST_DATABASE_URL` points at a
disposable `*_test` MySQL database (`tests/conftest.py` refuses any other name).
CI's `test-mysql` job does exactly that and then fails if any of the seven is
skipped.

### Dependency audit

Runtime (`requirements.txt`, resolved with its unpinned transitive packages):
clean.

Optional AI stack (`requirements-ai.txt`, **not** required to run the web
application; the explanation feature degrades to "unavailable" without it):

| Package | Pinned | Advisories | Fixed in | Upgrade available without breakage? |
| --- | --- | --- | --- | --- |
| torch | 2.10.0 | PYSEC-2025-194 (CVE-2025-3000, `torch.jit.script`), PYSEC-2026-139 (CVE-2026-4538, pt2 loading) | 2.13.0 / none listed | Not evaluated in M0 |
| transformers | 4.57.1 | 10 entries (7 unique CVEs: CVE-2025-14929, CVE-2025-14930, CVE-2026-1839, CVE-2026-4372, CVE-2026-5241, CVE-2026-9856, CVE-2026-80047) — deserialization/remote-code paths in model/checkpoint loading, `Trainer` RNG-state loading, a path traversal | 5.x (5.0.0 – 5.10.0); some have no fix listed | **No, not as a simple bump**: `requirements-ai.txt` documents that `peft==0.17.1` cannot import from transformers 5.x |
| accelerate | 1.14.0 | PYSEC-2026-3804 (CVE-2026-69112, checkpoint path traversal) | none listed | — |

Exposure in this application: the model and adapter are loaded from a local,
operator-controlled directory with `local_files_only=True` and the Hugging Face
offline flags set; no request input names a path or a model. The advisories are
mostly about loading **untrusted** checkpoints. That reduces, but does not
remove, the risk. **Decision for M0: record, do not upgrade.** The AI stack is
to be disabled by default in production (Phase 1), and the stack is revisited
as a whole (torch + transformers + peft together) before it is ever enabled for
a client. CI reports these advisories without failing.

### Dependency state (M0)

- `requirements.txt` and `requirements-ai.txt` pin direct dependencies with `==`.
- **Transitive dependencies are not pinned** (Werkzeug, Jinja2, WTForms,
  itsdangerous, …), so two installs on different days can differ. A
  hash-locked file is a Phase 4 task. No dependency was changed in M0.
- MySQL 8.0 (the tested server line) reached end of life in April 2026. MySQL
  8.4 LTS has **not** been tested.

---

## Unreleased changes since v0.9.0

| Item | Value |
| --- | --- |
| M1 — production admin bootstrap | `flask create-admin` (`app/cli.py`, `app/services/auth.create_admin`), 14 tests in `tests/test_create_admin.py` |
| Migration head | **`a7c2e9d4b1f3`** (12 linear revisions) — adds `admin_created` to the audit-event CHECK |
| Tests collected | **739** |
| SQLite | **732 passed, 7 skipped, 0 failed** (7 = MySQL-only concurrency tests) |
| MySQL 8.0 (disposable container) | **738 passed, 1 skipped, 0 failed** (skip = known issue #32); all 7 concurrency tests executed and passed |
| Dependencies | unchanged since v0.9.0 |
| M2 — Phase 1A security hardening | `ProxyFix` behind `TRUSTED_PROXY_COUNT` (default 0); customer checkout CUSTOMER-only, takeaway/online only, no table; reservation cap (`RESERVATION_ACTIVE_LIMIT`, default 2) under a row lock; business change + audit row in one transaction; `/account/me|staff|admin` removed; Argon2 rehash on login; failed-login audit stores a keyed identifier, not the username; `LLM_ENABLED` defaults to false. Details: `docs/SECURITY.md` §18 |
| Migration head after M2 | **`a7c2e9d4b1f3`** — unchanged (M2 needs no schema change) |
| Tests collected after M2 | **812** (72 new in `tests/test_m2_security.py`, 1 new MySQL race) |
| SQLite after M2 | **804 passed, 8 skipped, 0 failed** (8 = MySQL-only concurrency tests) |
| MySQL 8.0.46 after M2 (disposable container) | **811 passed, 1 skipped, 0 failed** (skip = known issue #32) |
| MySQL concurrency after M2 | **8 of 8 executed and passed** (stable over 5 consecutive runs) |
| M3 — staff order entry | `/staff/orders/new` (STAFF/ADMIN): dine-in or takeaway over the existing `checkout()`; seatable tables only (AVAILABLE/RESERVED, no open order), checked on a `FOR UPDATE`-locked row; server-side prices and 1–20 quantities; `order_created` audit in the same transaction. Details: `docs/SECURITY.md` §19 |
| Migration head after M3 | **`a7c2e9d4b1f3`** — unchanged (M3 needs no schema change) |
| Tests collected after M3 | **861** (48 new in `tests/test_m3_staff_orders.py`, 1 new MySQL race) |
| SQLite after M3 | **852 passed, 9 skipped, 0 failed** (9 = MySQL-only concurrency tests) |
| MySQL 8.0.46 after M3 (disposable container) | **860 passed, 1 skipped, 0 failed** (skip = known issue #32) |
| MySQL concurrency after M3 | **9 of 9 executed and passed** (stable over 5 consecutive runs) |
| M4 — account security & staff onboarding | password change (`/account/password`); admin account management (`/admin/accounts`: list, deactivate/activate, reset codes, staff invitations); one-time codes redeemed at `/account/setup`; public `/register/staff` removed; revocation via `session_version`; last-active-admin protection. Details: `docs/SECURITY.md` §20 |
| Migration head after M4 | **`e8b4d2a6c913`** (13 linear revisions) — adds `account_tokens` and 8 audit events |
| Tests collected after M4 | **922** (59 new in `tests/test_m4_account_security.py`, 2 new MySQL races) |
| SQLite after M4 | **911 passed, 11 skipped, 0 failed** (11 = MySQL-only concurrency tests) |
| MySQL 8.0.46 after M4 (local `quickjunction_test`) | **921 passed, 1 skipped, 0 failed** (skip = known issue #32) |
| MySQL concurrency after M4 | **11 of 11 executed and passed** (stable over 5 consecutive runs) |
| M5 — menu & restaurant foundation | single-row restaurant profile (`/admin/settings/restaurant`, audited) shown in the customer shell; category/item display order; Nepali names/descriptions with an English/नेपाली menu switcher and a vendored Devanagari font; staff sold-out control (`/staff/menu`); one public-menu rule (`public_menu_query`). Details: `docs/SECURITY.md` §21 |
| Migration head after M5 | **`c2d5f8a1b3e6`** (15 linear revisions) — `restaurant_profile`, `sort_order` columns, two translation tables, `restaurant_profile_changed` audit event |
| Tests collected after M5 | **1032** (108 in `tests/test_m5_catalog.py`, 1 in `tests/test_m5_migration.py`, 1 new MySQL race) |
| SQLite after M5 | **1020 passed, 12 skipped, 0 failed** (12 = MySQL-only concurrency tests) |
| MySQL 8.0.46 after M5 (local `quickjunction_test`) | **1031 passed, 1 skipped, 0 failed** (skip = known issue #32) |
| MySQL concurrency after M5 | **12 of 12 executed and passed**, stable over 5 consecutive runs |
| Dependencies after M5 | unchanged (the Devanagari font is a vendored static file, OFL-1.1) |
| Dependency audit (M2 re-run) | runtime: no known vulnerabilities; AI stack: same 13 advisories, accepted for the disabled-by-default optional model (`docs/SECURITY.md` §18.9) |
| Locale fix + M6.1 — customer design system | Fixed the unselected language-switcher button (dark on dark over hero photos) and a leak where a Nepali choice carried into the staff/admin console; then the Obsidian + Champagne customer design system: three-shell `base.html` (site/neutral/operations), mobile-first nav with a phone bottom bar, redesigned home/menu/dish pages. Presentation only: no route, model, migration, authorization, CSRF, availability, order or security-policy change. Details: `docs/PROJECT_PROGRESS.md` "M6.1 — Customer Design System Foundation" |
| Migration head after locale fix + M6.1 | **`c2d5f8a1b3e6`** — unchanged (presentation-only, no schema change) |
| SQLite after locale fix + M6.1 | **1071 passed, 12 skipped, 0 failed** (12 = MySQL-only concurrency tests) |
| MySQL 8.0.46 after locale fix + M6.1 (`quickjunction_test`) | **1082 passed, 1 skipped, 0 failed** (skip = known issue #32); all 12 concurrency tests executed and passed |
| M6.2 — responsive customer order layouts | Checkout's "Place order" button rebranded from the operations green to the customer brand colour; the three preference pages' headings standardized to the `qj-page-header`/`qj-eyebrow` pattern already used elsewhere; cart, checkout and order-history pages now render a second, mobile-only `.qj-panel` card layout (`d-md-none`) alongside the unchanged desktop `<table>` (`d-none d-md-block`), sharing the same forms, fields and CSRF tokens; the cart and checkout totals panels now use the existing `.qj-summary`/`.qj-summary__total` component instead of ad hoc markup. Presentation only: no route, model, migration, authorization, CSRF, pricing or order-logic change. Commits: `6357098`, `2e549022a0c43985f2719fb46d88a119b0e0c6a9` |
| Migration head after M6.2 | **`c2d5f8a1b3e6`** — unchanged (presentation-only, no schema change) |
| Tests collected after M6.2 | **1104** (11 new in `tests/test_m6_2_responsive.py`) |
| SQLite after M6.2 | **1092 passed, 12 skipped, 0 failed** — verified 2026-11-01 |
| MySQL after M6.2 | **not run.** The 12 MySQL-only concurrency tests have not been re-verified against a real MySQL instance since M6.2 landed; their last confirmed pass was at the locale fix + M6.1 state above |

---

## What exists today (verified against the code)

**Stack:** Flask 3.1 app factory, SQLAlchemy 2.0, Alembic via Flask-Migrate,
MySQL 8 (SQLite in tests), Jinja + vendored Bootstrap 5.3.3 (no CDN), Argon2id,
Flask-WTF CSRF, scikit-learn TF-IDF recommender, optional local Qwen3-0.6B +
LoRA **v4** explanations.

**Size:** 18 blueprints · 14 model modules / 14 tables · 17 service modules ·
51 templates · 32 audit event types · 7 order statuses · 4 order sources ·
3 roles (admin, staff, customer).

**Features present and covered by tests:** registration and login (three
sign-in portals), server-side session revocation, password change, staff
onboarding by admin invitation, admin account management with
reset codes and deactivation (M4), restaurant profile, English/Nepali menu content and staff sold-out control (M5), administrator creation via `flask create-admin` (CLI only), public menu with category filter and an in-page (client-side) dish
filter, session cart, checkout with server-side pricing and tax, order history
and customer cancellation, staff order workflow (atomic status transitions),
kitchen board, tables, reservations (fixed slots), payments and admin refunds,
staff/admin discounts, inventory (stock ledger, recipes, sale deduction), sales
reports, admin payments and audit views, preferences, deterministic
recommendations, role dashboards, security headers (CSP, anti-framing,
no-store), audit log, responsive mobile card layouts for cart/checkout/order
history alongside the unchanged desktop tables (M6.2).

**Local AI:** default adapter is
`models/qwen3-0.6b-quickjunction-lora-v4` (`config.py`), behind a deterministic
preference-safety guard. Weights (~1.3 GB) are git-ignored and must be copied
separately. Since M2 the LLM is **disabled by default in every environment**;
`LLM_ENABLED=true` opts in.

## What does NOT exist (do not assume it does)

Production deployment configuration (WSGI service, reverse proxy, TLS,
systemd, Docker) · admin user
management (deactivate, reset, role change) · password change / reset · email
of any kind · multi-worker-safe rate limiting · automated backups,
restore scripts · upgrade/rollback tooling · version endpoint · pagination on
the staff order queue, customer history, audit and payment views · server-side
menu search · order notes · delivery address · receipts · restaurant
configuration (slots, currency, branding are code constants) · notifications ·
online payment gateway · multi-restaurant support · load testing / measured
capacity.

**No capacity claim is made.** Concurrent-user capacity and order throughput
are UNKNOWN until Phase 5 measures them.

## Open known issues (current)

Numbering follows `docs/PROJECT_PROGRESS.md` "Known issues".

| # | Issue |
| --- | --- |
| 3 | Anonymous cart discarded on login |
| 4 | Checkout idempotency relies on cart clearing, no idempotency key |
| 5 | Login rate limiter is in-process only (`ProxyFix` added in M2; set `TRUSTED_PROXY_COUNT` behind a proxy) |
| 8 | No audit-log retention job |
| 12 / 39 | Unpaginated staff queue, customer history, admin payments/audit (200-row cap) |
| 13 | `migrations/env.py` uses deprecated `get_engine()` (breaks on Flask-SQLAlchemy 3.2) |
| 14–18 | Recommendation-engine limits (re-vectorised per request, cold start, coarse matching) |
| 20a, 21–24, 27 | Local-LLM limits: synchronous CPU generation of several seconds, one model copy per worker |
| 26a | Logout revokes every session of the account |
| 28 | No production deployment configuration |
| 29 | Test fixture keeps one app context per test (identity can leak between simulated users) |
| 32 | Full downgrade to base fails on MySQL (pre-M10 revisions) |
| 35 / 36 | Stock checked not reserved; no ingredient costs |
| 37 | No order notes |
| — | Runtime DB account also holds DDL privileges; from the 2026-10-03 audit; scheduled for Phase 1. Customer dine-in and the reservation cap were closed in M2; public `/register/staff` was removed in M4 |
| — | No "add items to an open order": a seated table takes one open order at a time (M3), so a party's follow-up order needs a new order once the table is freed |
| — | AI-stack dependency advisories (above) |
| — | No partial/split payment ledger or outstanding-balance workflow: `payments.order_id` is unique, so an order is paid in full in one transaction or not at all; refunds alone can be partial. From the 2026-11-01 project health audit; a product-scope decision, not yet made |
| — | No automated backup/restore tooling exists (see "What does NOT exist" above); from the 2026-11-01 audit, which also re-confirmed the inventory check-then-deduct race (#35/36) and the in-process-only rate limiter (#5) are both still open |

---

## Next planned milestone

**M7 — Production Reliability Foundation** (not started). Addresses the
highest-severity gaps found in the 2026-11-01 project health audit, in this
proposed order: (1) automated database backup and a tested restore
procedure; (2) separate the database runtime account (DML only) from a
migration-only account (DDL), so the application can no longer
`ALTER`/`DROP` a table; (3) close the inventory stock-reservation race
between the shortage check and `deduct_for_order()`; (4) replace the
in-process rate limiter with a shared-store implementation, once a
multi-worker deployment is planned. Deliberately not a new customer-facing
feature — the audit's own instruction was not to add features before
resolving foundational reliability issues. No task has started.
