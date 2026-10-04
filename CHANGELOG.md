# Changelog

All notable changes to Quick Junction are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/). Before 1.0.0 a
minor release may contain breaking changes; 1.0.0 will be the first release
approved for a pilot restaurant.

Each release uses only the sections that apply, in this order:

- **Added** — new features.
- **Changed** — changes to existing behaviour.
- **Fixed** — bug fixes.
- **Security** — vulnerability fixes and hardening.
- **Migration** — database schema changes: revision ids, whether the
  migration is reversible, expected duration on a large database.
- **Operator action required** — anything a person running an installation
  must do (new environment variables, services, manual steps, config
  changes). If this section is absent, an upgrade needs no manual step beyond
  the standard upgrade procedure.

## [Unreleased]

### Added

- **`flask create-admin`** (M1): the production way to create an
  administrator. Interactive only — prompts for username, email and password
  (hidden, entered twice); refuses to run without a terminal and never takes
  the password as an argument or from piped input. Reuses the web
  registration rules (username pattern, email syntax, password policy,
  duplicate checks) and Argon2id hashing. The account and its audit row are
  written in one transaction. Documented in `docs/DATABASE_SETUP_GUIDE.md` §14.
- `admin_created` audit event, recorded by `flask create-admin` (metadata:
  `{"source": "cli"}` only).
- `TRUSTED_PROXY_COUNT` setting (M2, default `0`): Werkzeug `ProxyFix` is
  applied only when it is positive, trusting exactly that many proxies for
  `X-Forwarded-For`/`-Proto`/`-Host`.
- `RESERVATION_ACTIVE_LIMIT` setting (M2, default `2`): most active future
  CONFIRMED reservations per customer, enforced under a `users`-row lock.
- `app/services/audit.commit_with` and `login_identifier` (M2).
- `tests/test_m2_security.py` (72 tests) and a MySQL reservation-cap race
  test; CI now expects 8 MySQL concurrency tests.
- `docs/STATUS.md` — verified current state and baseline record.
- `docs/PRODUCTION_READINESS.md` — go-live checklist.
- `docs/RELEASE_PROCESS.md` — branching, tagging, changelog and intended
  `main` branch protection rules.
- GitHub Actions CI (`.github/workflows/ci.yml`): full test suite on SQLite and
  on a disposable MySQL 8.0 service (the seven MySQL concurrency tests must
  execute, not skip), plus `pip-audit`.
- This changelog.

### Changed

- `docs/PROJECT_PROGRESS.md` and `docs/SECURITY.md`: stale and contradictory
  statements corrected (migration head, production adapter, implemented
  features, closed known issues). Both now defer to `docs/STATUS.md` for the
  current state.
- `README.md`: "Project status" no longer says "feature-complete and
  released"; it states the v0.9.0 not-production-ready baseline.

- `app/services/audit.record_event` accepts `commit=False` so an audit row can
  share the caller's transaction (used by `create_admin`; every existing caller
  keeps the previous behaviour).
- **M2:** `record_event` no longer commits by default. Business actions write
  their audit row inside their own transaction; independent auth/rejection
  events pass `commit=True`.
- **M2:** `/checkout` is for CUSTOMER accounts only (staff/admin get 403) and
  offers takeaway and online only. Dine-in and delivery are no longer
  available to customers; the table selector is gone.
- **M2:** `LLM_ENABLED` defaults to `false` in every environment, development
  included.
- **M2:** failed-login and rate-limited audit rows store
  `{"login_id": <keyed hash>}` instead of the typed username.

### Removed

- **M2:** `/account/me`, `/account/staff`, `/account/admin` probe endpoints.

### Security

- **M2 — Phase 1A hardening** (`docs/SECURITY.md` §18): forwarded headers
  untrusted unless configured; customers can no longer occupy, reserve or
  otherwise change a table through checkout; staff/admin cannot place orders
  through the customer checkout; per-customer reservation cap that holds
  under concurrency; business changes can no longer commit without their audit
  row; outdated Argon2 hashes are upgraded on successful login; raw login
  input is no longer stored in the audit log; the local LLM is off unless
  explicitly enabled. Dependency audit re-run: runtime clean, optional AI
  stack unchanged (13 advisories, accepted while disabled).

### Fixed

- `README.md`: the `scripts\reset_database.bat` path was corrupted (a
  carriage-return byte had replaced `\r`).

### Migration

- `a7c2e9d4b1f3` (revises `f3a6d9b2e8c5`): widens the `ck_audit_logs_event_type`
  CHECK constraint to allow `admin_created`. No table or column change; quick on
  any database size. Reversible: `downgrade()` deletes `admin_created` audit rows
  (the admin accounts themselves are kept) and restores the previous list.

### Operator action required

- Run `flask db upgrade` (new head `a7c2e9d4b1f3`). Until it is applied,
  `flask create-admin` fails with a CHECK-constraint error and creates nothing.
- Create administrators with `flask create-admin` from an interactive terminal on
  the server. Do not promote accounts with SQL.
- **M2 (no migration):** behind a reverse proxy, set `TRUSTED_PROXY_COUNT`
  to the number of proxies (1 for a single Nginx), or every client will share
  the proxy's IP. Leave it at 0 otherwise.
- **M2:** to keep AI explanations on (e.g. a development machine or demo), set
  `LLM_ENABLED=true`. They are now off by default.
- **M2:** staff can no longer take dine-in orders through `/checkout`, and
  there is no replacement screen yet. Plan pilot operations accordingly.

## [0.9.0] — 2026-10-03

**Quick Junction v0.9.0 — audited prototype baseline; not production ready.**

This release records the state of commit `201f55a` as audited on 2026-10-03.
It is a **prototype / pre-production baseline** and is **NOT approved for
production deployment**. See `docs/STATUS.md` for what exists and
`docs/PRODUCTION_READINESS.md` for what must be completed before go-live.

### Added

Everything present in the repository at `201f55a`, summarised (details in
`docs/PROJECT_PROGRESS.md`):

- Accounts: customer registration, three sign-in portals, staff sign-up with
  admin approval, Argon2id password hashing, server-side session revocation.
- Menu: categories and items with admin management, public browsing.
- Ordering: session cart, checkout with server-side pricing and tax, order
  history, customer cancellation.
- Operations: staff order workflow, kitchen board, tables, reservations,
  payments and admin refunds, discounts, inventory with a stock ledger.
- Reporting: sales reports, admin payments and audit-log views, role
  dashboards.
- Personalisation: preferences, deterministic TF-IDF recommendations, optional
  local Qwen3-0.6B + LoRA v4 explanations.
- Security controls: CSRF, role checks on every route, owner-scoped customer
  queries, CSP and anti-framing headers, audit log.
- Windows development setup scripts.

### Migration

- Schema head `f3a6d9b2e8c5` (11 linear revisions). A full downgrade to base
  fails on MySQL (known issue #32).

### Operator action required

- **Do not deploy this version to a real restaurant.** There is no production
  deployment configuration, no way to create the first admin in production, no
  automated backup, and no upgrade or rollback procedure.
