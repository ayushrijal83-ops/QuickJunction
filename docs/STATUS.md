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
sign-in portals), server-side session revocation, staff sign-up with admin
approval, administrator creation via `flask create-admin` (CLI only), public menu with category filter and an in-page (client-side) dish
filter, session cart, checkout with server-side pricing and tax, order history
and customer cancellation, staff order workflow (atomic status transitions),
kitchen board, tables, reservations (fixed slots), payments and admin refunds,
staff/admin discounts, inventory (stock ledger, recipes, sale deduction), sales
reports, admin payments and audit views, preferences, deterministic
recommendations, role dashboards, security headers (CSP, anti-framing,
no-store), audit log.

**Local AI:** default adapter is
`models/qwen3-0.6b-quickjunction-lora-v4` (`config.py`), behind a deterministic
preference-safety guard. Weights (~1.3 GB) are git-ignored and must be copied
separately. The LLM is disabled in tests and enabled by default in development.

## What does NOT exist (do not assume it does)

Production deployment configuration (WSGI service, reverse proxy, TLS,
systemd, Docker) · admin user
management (deactivate, reset, role change) · password change / reset · email
of any kind · multi-worker-safe rate limiting · `ProxyFix` · automated backups,
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
| 5 | Login rate limiter is in-process only; no `ProxyFix` (behind a proxy every client shares one IP) |
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
| — | Runtime DB account also holds DDL privileges; public `/register/staff`; customer dine-in can mark tables occupied; no per-customer reservation cap (all from the 2026-10-03 audit; scheduled for Phase 1) |
| — | AI-stack dependency advisories (above) |
