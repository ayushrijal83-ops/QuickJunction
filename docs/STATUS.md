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
| M7.4 — inventory concurrency fix (closes known issue #35) | Commit `67bbb74`, 2026-11-02; not recorded in this table when it shipped. `deduct_for_order` now locks every needed ingredient (`SELECT ... FOR UPDATE`, ascending id order) at order completion and refuses the whole `→ COMPLETED` transition if stock is short, instead of letting a served order drive stock negative. Checkout's `shortages()` check is unchanged and explicitly advisory-only. Details: `docs/PROJECT_PROGRESS.md` "M7.4 — Inventory Concurrency Fix" |
| Migration head after M7.4 | unchanged (no schema change — the fix is locking + control flow only) |
| Tests after M7.4 | `tests/test_inventory.py::test_completion_refused_when_stock_is_insufficient` (replaces a test asserting the old behavior) + new MySQL-only race `tests/test_mysql_concurrency.py::test_two_orders_racing_to_complete_cannot_oversell_a_scarce_ingredient`; CI's concurrency guard count moved 12 → 13 at this commit (now 15, see M7.5.4.7 below) |
| M7.5.4 — payment-ledger legacy-data reconciliation utility | Builds on the M7.5.1 payment ledger foundation, M7.5.2 ledger-backed payment/refund service, and M7.5.3 live route cutover (all shipped since this table's M6.2 entry above, undocumented here until now). `scripts/reconcile_payment_ledger.py`: a standalone, dry-run-by-default, `--apply --confirm "RECONCILE <n>"`-gated operator tool that backfills a ledger entry for any order paid through the legacy route after the M7.5.1 migration ran but before the M7.5.3 cutover deployed (see `app/services/payment_ledger.py`'s "Cross-path note"). One order per transaction, lock-then-reverify, never an unconditional settlement overwrite; an inconsistent record is reported for manual review, never guessed at. Details: `docs/PRODUCTION_READINESS.md` P7 |
| Tests collected after M7.5.4 (**interim checkpoint, superseded by M7.5.4.7 below -- not the current total**) | **1279** (13 new in `tests/test_payment_ledger_reconciliation.py`, 1 new MySQL race in `tests/test_mysql_concurrency.py`) |
| SQLite after M7.5.4 | **1258 passed, 21 skipped, 0 failed** |
| MySQL after M7.5.4 | **not run against a real installation.** The script and its tests have been exercised locally against SQLite only, plus one opt-in test/race written for `TEST_DATABASE_URL` MySQL but not executed in this session (no local MySQL available) -- **no production reconciliation has been performed** |
| M7.5.4.7 — retire legacy payment writers | `app/services/payments.py` (the legacy `record_payment`/`refund_payment`, unreachable from any live route since M7.5.3) is **deleted outright**, closing the post-commit race a legacy refund could otherwise win after reconciliation already committed. `app/services/payment_ledger.py` is now the *only* code that can write a payment or refund, in production or in tests. The legacy `payments` table, its model, and `scripts/reconcile_payment_ledger.py` are unchanged -- historical rows remain readable and reconcilable exactly as before. Tests that exercised the retired functions' own business rules (double-payment/refund-bound/stale-refund concurrency) were removed as redundant with the equivalent, already-existing `test_payment_ledger_service.py` coverage for the ledger-backed path; tests that used the legacy functions only as fixtures to build a "legacy-paid, pre-ledger order" (the UI reconciliation guard, the reconciliation script's own tests, the M7.5.4.4 stale-read regression) were rewritten against a new `make_legacy_payment`/`refund_legacy_payment` test fixture in `tests/conftest.py` that inserts the same row shape directly. Details: `docs/PROJECT_PROGRESS.md` "M7.5.4.7". |
| Tests collected after M7.5.4.7 | **1269** (11 legacy-writer-specific tests removed from `tests/test_payments.py`; 2 redundant legacy-writer concurrency tests removed from `tests/test_mysql_concurrency.py`, now 15) |
| SQLite after M7.5.4.7 | **1249 passed, 20 skipped, 0 failed** |
| MySQL after M7.5.4.7 | **not run against a real installation** (same as M7.5.4 -- no local MySQL server available this session); **no production reconciliation has been performed** |
| M7.5.4.9 — payment-ledger integrity audit (standing, read-only) | Commit `d425b72`, 2026-11-07; not recorded in this table when it shipped. `scripts/audit_payment_ledger.py` -- a **permanent** operator/CI health check (not a one-time migration tool like M7.5.4): checks the ledger's internal consistency and its agreement with `Order.amount_settled` and the legacy `payments` table; never writes anything. Exit codes: 0 clean, 1 finding or malformed/disallowed target, 2 no database URL given. Details: `docs/PROJECT_PROGRESS.md` "M7.5.4.9" |
| Tests collected after M7.5.4.9 | **+24** (`tests/test_payment_ledger_audit.py`) |
| M7.5.5.1 — real-schema backup → restore → payment-ledger-audit drill | Commits `c048e8f` (added, 2026-11-08), `3b9cd36` (reverted same day -- MySQL CI job hung reproducibly on 2 consecutive runs, ~38 min with no output before the 45-min timeout), `3fb5c7d` (re-added with the metadata-lock issue fixed, 2026-11-08). Proves the *actual* schema (orders/payments/payment_ledger_entries, real FKs and CHECKs) round-trips through a real `mysqldump`/restore cycle and still passes the M7.5.4.9 audit afterward -- closing the one gap `test_live_backup_and_restore_drill`'s single custom table never covered. Details: `docs/PROJECT_PROGRESS.md` "M7.5.5.1" |
| Tests collected after M7.5.5.1 | **1314** (+1 MySQL-only, `tests/test_payment_ledger_backup_restore_drill.py`) |
| SQLite at this checkpoint (commit `3fb5c7d`) | **1286 passed, 28 skipped, 0 failed** -- CI run `42489009615`, confirmed SUCCESS via `gh run view` |
| MySQL 8.0 at this checkpoint | **1307 passed, 7 skipped, 0 failed** -- same CI run; concurrency guard **15 passed** (not skipped) |

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
of any kind · multi-worker-safe rate limiting · scheduled (unattended)
backups, backup failure alerting, off-machine backup copies, backup key
escrow and a disaster-recovery runbook (the backup and restore *scripts*
themselves exist since M7.2 — see the known-issues table below) ·
upgrade/rollback tooling · version endpoint · pagination on
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
| ~~35~~ | ~~Stock checked not reserved~~ — **closed in M7.4** (2026-11-02, commit `67bbb74`): completion now locks and refuses under shortage; see `docs/PROJECT_PROGRESS.md` "M7.4" |
| 36 | No ingredient costs (unrelated to #35; separate, still-open valuation gap) |
| 37 | No order notes |
| — | Database privilege separation is implemented but not yet proven live (was: "Runtime DB account also holds DDL privileges", 2026-10-03 audit). M7.3 (commit `9160305`): `scripts/db_setup.py setup` creates a DML-only runtime account (`qj_user`: `SELECT, INSERT, UPDATE, DELETE`) and a separate `qj_migrator` account for migrations, used through `MIGRATION_DATABASE_URL` (`migrations/env.py`). Still open: (a) an installation created before M7.3 keeps its DDL grants until an operator runs the opt-in `db_setup.py narrow-runtime` — it is never run automatically; (b) the live drill `tests/test_privilege_separation.py::test_live_privilege_separation_drill` has not been executed against a real server — it was skipped in CI run `42489009615` because it needs `QJ_MYSQL_ADMIN_PASSWORD`; the separation is proven by mocked tests only. `docs/PRODUCTION_READINESS.md` S10 stays IN PROGRESS. Customer dine-in and the reservation cap were closed in M2; public `/register/staff` was removed in M4 |
| — | No "add items to an open order": a seated table takes one open order at a time (M3), so a party's follow-up order needs a new order once the table is freed |
| — | AI-stack dependency advisories (above) |
| — | No partial/split payment ledger or outstanding-balance workflow: `payments.order_id` is unique, so an order is paid in full in one transaction or not at all; refunds alone can be partial. From the 2026-11-01 project health audit; a product-scope decision, not yet made |
| — | Backups are not yet operational, although the tooling exists (was: "No automated backup/restore tooling exists", 2026-11-01 audit). M7.2 (commit `75ab4b8`) delivered `scripts/backup_database.py` (`mysqldump --single-transaction`, atomic write, SHA-256 checksum, Fernet encryption by default, count-based retention) and `scripts/restore_database.py` (checksum verification, refuses a non-empty target, post-restore row-count check). `tests/test_backup_restore.py::test_live_backup_and_restore_drill` and the M7.5.5.1 real-schema drill both ran, not skipped, on MySQL 8.0 in CI run `42489009615`. Still open: nothing runs a backup unattended (the `schtasks`/cron commands are documented in `docs/DATABASE_SETUP_GUIDE.md` §19 but not registered anywhere); no success/failure alerting; no off-machine copy; no key escrow; no disaster-recovery runbook; no drill against a real installation, because none exists yet. `docs/PRODUCTION_READINESS.md` P1–P6. The same 2026-11-01 audit re-confirmed the in-process-only rate limiter (#5), which is still open; its inventory check-then-deduct race (#35) was closed by M7.4 (commit `67bbb74`) — see above |

---

## Next planned milestone

**M7 — Production Reliability Foundation.** Addressed the highest-severity
gaps found in the 2026-11-01 project health audit, in this order: (1)
automated database backup and a tested restore procedure — **tooling
delivered, M7.2** (backup/restore scripts and a CI-run restore drill;
unattended scheduling, alerting and off-machine copies are still open);
(2) separate the database runtime account (DML only) from a migration-only
account (DDL) — **implemented, M7.3** (the live privilege drill has not
been run and pre-M7.3 installations need the opt-in `narrow-runtime`);
(3) close the inventory stock-reservation race between the shortage check
and `deduct_for_order()` — **done, M7.4** (2026-11-02, commit `67bbb74`;
closes known issue #35 above). The code for (1)–(3) has shipped; the
remaining gaps for (1) and (2) are operational and are listed in the
known-issues table above. This paragraph described all of M7 as "not
started" until the 2026-11-10 documentation reconciliation.

**Remaining: (4) replace the in-process rate limiter (S1) with a
shared-store implementation**, once a multi-worker deployment is planned.
This is the one item from the original M7 list not yet started, and is the
next scoped piece of work. Deliberately not a new customer-facing feature
— the audit's own instruction was not to add features before resolving
foundational reliability issues.
