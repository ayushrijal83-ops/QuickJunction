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
| S1 — shared rate limiting | **S1-A** (commit `60195ae`): Redis sliding-window limiter, per-process fallback, `REDIS_URL` required in production; CI green on SQLite, MySQL 8.0, `redis:7.4` and the dependency audit. **S1-B** (commit `9ca483b`, verified and closed): the limiter on login, password change, registration, account setup, checkout, reservations and AI explanation; login and password change moved to atomic reserve-then-resolve; login refuses usernames outside `USERNAME_PATTERN` (MySQL accent-variant bypass). Details: `docs/SECURITY.md` §22, `docs/PROJECT_PROGRESS.md` "S1-B" |
| Tests after S1-B | +41 `tests/test_ratelimit_routes.py`, +9 `tests/test_ratelimit_redis.py` (now 23; CI guard updated to 23), +12 `tests/test_login_username_pattern.py`, +3 MySQL-only `tests/test_mysql_username_collation.py` (new CI guard: all 3 must pass) |
| SQLite after S1-B (local, Windows, Python 3.10) | **1404 passed, 49 skipped, 0 failed** (including the pre-CI corrections) — skips are the 23 Redis tests (no `TEST_REDIS_URL`) and 26 MySQL-only tests, 3 of them the new username-collation tests |
| S1-B CI verification | CI run `43231240136` on commit `9ca483bba7256434ab1e6a3201ce83be3ac96a3f` — **SUCCESS**, all four jobs green, 0 failed anywhere |
| Redis 7.4 after S1-B (CI `test-redis`) | **23 passed, 0 skipped** against a real `redis:7.4` service (guard requires exactly 23); the same job's in-memory limiter (60) and route (41) tests also passed |
| MySQL 8.0 after S1-B (CI `test-mysql`) | **1423 passed, 30 skipped, 0 failed**; concurrency guard **15 passed**; username-collation guard **3 passed** (B1 proven on MySQL's own collation). Expected skips, not failures: 23 Redis tests (no Redis service in this job), 5 AI-model tests (`models/` is git-ignored), 1 known issue #32 migration test, 1 privilege-separation drill (needs `QJ_MYSQL_ADMIN_PASSWORD`, not provided in CI) |
| SQLite after S1-B (CI `test-sqlite`) | **1399 passed, 54 skipped, 0 failed**. Expected skips, not failures: 23 Redis tests, 26 MySQL-only tests (incl. the 3 collation and 15 concurrency tests, which run in the MySQL job), 5 AI-model tests. The local count (1404 / 49) differs only by those 5 model tests, which run where `models/` exists |
| Dependency audit after S1-B | **passed** (`pip-audit`, blocking for `requirements.txt`) |
| O1 — local organization & branch identity | **Implemented and CI-verified** (commit `44e63c2`, CI run `43347276196`, all four jobs green). Three additive tables -- `organizations`, `branches`, `installation` (singleton, id = 1) -- in `app/models/installation.py`; migration **`4c7e2a9f1b38`** creates them and inserts exactly one Organization, one Branch and one Installation, named after `restaurant_profile.name`, with locally generated UUIDs and NULL `hq_id`s. Read-only `get_installation()` (`app/services/installation.py`) and `flask installation show`. No operational table, route, role or authorization changed. Details: `docs/PROJECT_PROGRESS.md` "O1" |
| Migration head after O1 | **`4c7e2a9f1b38`** (revises `e39e6dffcb5d`) |
| Tests after O1 | +27: `tests/test_installation.py` (21, all backends), `tests/test_installation_migration.py` (5, one MySQL-only), `tests/test_installation_backup_restore.py` (1, MySQL-only); new CI guard in `test-mysql`: exactly 6 O1 tests must pass, none skipped |
| SQLite after O1 (local, Windows, Python 3.10) | **1429 passed, 51 skipped, 0 failed** (S1-B baseline 1404 / 49: +25 passed, +2 MySQL-only skips) |
| O1 CI verification | CI run `43347276196` on commit `44e63c2205dbadea2a08983db40fd4ba76076014` -- **SUCCESS**, all four jobs green, 0 failed anywhere |
| MySQL 8.0 after O1 (CI `test-mysql`) | **1450 passed, 30 skipped, 0 failed** (S1-B: 1423 / 30 -- +27 passed, no new skips: all 27 O1 tests executed on MySQL). O1 guard **6 passed, 0 skipped**; concurrency guard **15 passed**; username-collation guard **3 passed**. The 30 skips are unchanged from S1-B: 23 Redis tests, 5 AI-model tests, 1 known issue #32 migration test, 1 privilege-separation drill |
| SQLite after O1 (CI `test-sqlite`) | **1424 passed, 56 skipped, 0 failed** (S1-B: 1399 / 54 -- +25 passed, +2 skips: the two MySQL-only O1 tests) |
| Redis and dependency audit after O1 | Redis job passed (23 Redis tests passed, guard requires exactly 23; in-memory limiter 60 and route 41 passed); `pip-audit` passed |
| MySQL 8.0 locally | not run -- no `TEST_DATABASE_URL` on the development machine; the CI job above is the MySQL evidence |
| O2 — organization membership & branch assignment | **Implemented and CI-verified** (commit `080fd5b`, CI run `43356441796`, all four jobs green). Two additive tables, `organization_memberships` and `branch_assignments` (`app/models/organization.py`), each row `active` or `revoked`, plus a unique index on O1's `branches (id, organization_id)` (no table rebuild). Composite foreign keys make a cross-organization assignment impossible at the database level. Migration **`4f1d8b2c6a90`** gives every existing ADMIN and STAFF account -- including inactive, pending and invited staff -- one active membership and one active assignment to the local branch; customers get neither; no account field changes and no owner is chosen; it refuses (changing nothing) unless the database holds exactly one organization and one branch. `create_admin()`, `invite_staff()` and `scripts/seed_demo.py` now enroll new staff/admin accounts in the same transaction. Service `app/services/organization.py` (add/assign/revoke/reinstate, all audited atomically) and read-only `flask organization members`. **Membership grants no access**: sign-in and every permission check are unchanged and still read only `users.role`, `is_active`, `staff_approved`. No UI or write command. Details: `docs/PROJECT_PROGRESS.md` "O2" |
| Migration head after O2 | **`4f1d8b2c6a90`** (revises `4c7e2a9f1b38`) |
| Tests after O2 | +45: `tests/test_organization.py` (40, one MySQL-only), `tests/test_organization_migration.py` (5, one MySQL-only); new CI guard in `test-mysql`: exactly 45 O2 tests must pass, none skipped. Existing tests adjusted: `tests/test_foundation.py` (+2 tables), `tests/test_m5_migration.py` (head), `tests/test_installation_migration.py` (pinned to O1's revision), and the opt-in `installation_identity` fixture added to the account-creation tests in `test_create_admin.py`, `test_m4_account_security.py`, `test_demo_flow.py`, `test_ratelimit_routes.py` |
| SQLite after O2 (local, Windows, Python 3.10) | **1472 passed, 53 skipped, 0 failed** (O1 baseline 1429 / 51: +43 passed, +2 MySQL-only skips) |
| O2 CI verification | CI run `43356441796` on commit `080fd5b7b6b3cef7a4744cd9c57a4f87719cbd2b` -- **SUCCESS**, all four jobs green, 0 failed anywhere |
| MySQL 8.0 after O2 (CI `test-mysql`) | **1495 passed, 30 skipped, 0 failed** (O1: 1450 / 30 -- +45 passed, no new skips: all 45 O2 tests executed on MySQL, including the migration, populated backfill, refusal, downgrade/re-upgrade and database-constraint tests). O2 guard **45 passed, 0 skipped**; O1 guard **6 passed**; concurrency guard **15 passed**; username-collation guard **3 passed**. The 30 skips are unchanged: 23 Redis tests, 5 AI-model tests, 1 known issue #32 migration test, 1 privilege-separation drill |
| SQLite after O2 (CI `test-sqlite`) | **1467 passed, 58 skipped, 0 failed** (O1: 1424 / 56 -- +43 passed, +2 skips: the two MySQL-only O2 tests) |
| Redis and dependency audit after O2 | Redis job passed (23 Redis tests passed, guard requires exactly 23; in-memory limiter 60 and route 41 passed); `pip-audit` passed |
| MySQL 8.0 locally after O2 | not run -- no `TEST_DATABASE_URL` on the development machine; the CI job above is the MySQL evidence |
| O3.1 — authorization regression baseline (test-only) | **Implemented and CI-verified** (commit `7252adb`, CI run `43434000168`, all four jobs green). The first step of O3 (organization and branch permissions). It pins the **current** authorization behaviour before permission-based checks replace the global-role checks: every one of the 91 served (URL rule, method) pairs against eight account classes (anonymous, customer, staff, admin, inactive staff, inactive admin, pending staff, and an admin with `staff_approved` false), using valid data; sign-in by account class; customer ownership of orders and reservations; and the sensitive service functions called directly. Services that have no authorization check of their own are pinned as `test_gap_*` findings that assert the call *succeeds*; a later O3 milestone that adds the check must change the matching test on purpose. **No production code, model, migration, template, configuration or authorization behaviour changed**, and O3 permission enforcement has **not** started. Details: `docs/PROJECT_PROGRESS.md` "O3.1" |
| Migration head after O3.1 | **`4f1d8b2c6a90`** -- unchanged (test-only) |
| Tests after O3.1 | +856: `tests/test_authz_baseline_routes.py` (809), `tests/test_authz_baseline_services.py` (47), and the helper `tests/authz_baseline_world.py` (not a test module). None is backend-specific, so all 856 run on SQLite and MySQL. No new CI guard; no existing test changed |
| SQLite after O3.1 (local, Windows, Python 3.10) | **2328 passed, 53 skipped, 0 failed** (O2 baseline 1472 / 53: +856 passed, no new skips) |
| O3.1 CI verification | CI run `43434000168` on commit `7252adbd7dcdfcd7b291cf1a6bfeae6c14139de0` -- **SUCCESS**, all four jobs green, 0 failed anywhere |
| MySQL 8.0 after O3.1 (CI `test-mysql`) | **2351 passed, 30 skipped, 0 failed** (O2: 1495 / 30 -- +856 passed, no new skips). O2 guard **45 passed**; O1 guard **6 passed**; concurrency guard **15 passed**; username-collation guard **3 passed**. The 30 skips are unchanged. The job took about 27 minutes (O2: about 18) against its 45-minute timeout |
| SQLite after O3.1 (CI `test-sqlite`) | **2323 passed, 58 skipped, 0 failed** (O2: 1467 / 58 -- +856 passed, no new skips) |
| Redis and dependency audit after O3.1 | Redis job passed (23 Redis tests passed, guard requires exactly 23; in-memory limiter 60 and route 41 passed); `pip-audit` passed |
| MySQL 8.0 locally after O3.1 | not run -- no `TEST_DATABASE_URL` on the development machine; the CI job above is the MySQL evidence |
| Open before O3 membership enforcement: pending ADMIN | **Not resolved.** Today `staff_approved = false` blocks only STAFF; an ADMIN with `staff_approved = false` signs in and has full admin access, and `flask create-admin` creates every admin that way. O3.1 pins this as existing behaviour (the `pending_admin` class). The approved O3 policy -- pending accounts get no workforce access, and the O3 migration refuses pending workforce accounts -- would, read literally, refuse or lock out every existing admin. What "pending" means for ADMIN must be decided before membership enforcement |

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
of any kind · a deployed Redis server for the shared rate limiter (the
code exists since S1-A/S1-B; no installation has one) · scheduled (unattended)
backups, backup failure alerting, off-machine backup copies, backup key
escrow and a disaster-recovery runbook (the backup and restore *scripts*
themselves exist since M7.2 — see the known-issues table below) ·
upgrade/rollback tooling · version endpoint · pagination on
the staff order queue, customer history, audit and payment views · server-side
menu search · order notes · delivery address · receipts · restaurant
configuration (slots, currency, branding are code constants) · notifications ·
online payment gateway · multi-restaurant support · load testing / measured
capacity · HQ registration or activation, any cloud service, branch
synchronization, an organization portal, multi-branch staff management, an
Organization Owner role and license enforcement (O1 adds only a *local*
organization/branch/installation identity; `hq_id` columns exist but nothing
sets them) · organization or branch roles and any permission that depends on
membership (O2 records organization membership and branch assignment, but
they grant and revoke no access; there is no UI or write command for them) ·
permission-based authorization (O3.1 only pins today's role-based checks in
tests; every gate still reads `users.role`, `is_active` and `staff_approved`).

**No capacity claim is made.** Concurrent-user capacity and order throughput
are UNKNOWN until Phase 5 measures them.

## Open known issues (current)

Numbering follows `docs/PROJECT_PROGRESS.md` "Known issues".

| # | Issue |
| --- | --- |
| 3 | Anonymous cart discarded on login |
| 4 | Checkout idempotency relies on cart clearing, no idempotency key |
| 5 | ~~Login rate limiter is in-process only~~ — addressed in code by S1-A (shared Redis limiter, commit `60195ae`) and S1-B (seven routes on it, commit `9ca483b`, CI run `43231240136` green including real `redis:7.4` and MySQL 8.0). Remaining: no Redis deployed anywhere; degraded mode during a Redis outage is per-worker (`docs/SECURITY.md` §22.6). Set `TRUSTED_PROXY_COUNT` behind a proxy |
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

**O3.2 — Permission Foundation.** Not started; it waits for review and
explicit approval. O3 replaces the global-role checks with organization and
branch permissions in steps. O3.1 (above) is the regression baseline those
steps must keep green, changing a pinned test only on purpose. Before O3
enforces membership, the pending-ADMIN question above must be settled.

The M7 and S1 paragraphs below are the earlier plan, kept as written. Their
code has shipped, and their remaining gaps are operational.

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

**(4) replace the in-process rate limiter (S1) with a shared-store
implementation** — done in code and CI-verified. S1-A (commit `60195ae`,
CI green including `redis:7.4`) built the Redis-backed limiter; S1-B (commit
`9ca483b`, CI run `43231240136` green on SQLite, MySQL 8.0, `redis:7.4` and
the dependency audit) integrates it into login, registration, account setup,
password change, checkout, reservations and AI explanation
(`docs/SECURITY.md` §22.9). What remains is operational: no Redis server is
deployed anywhere yet. See "S1 — shared rate limiting" in the
unreleased-changes table.
