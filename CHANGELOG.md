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
- **Staff order entry** (M3, `/staff/orders/new`, STAFF/ADMIN): dine-in or
  takeaway, eligible tables only, "Review totals" then "Create order", built
  on the existing `checkout()`. "New order" button on the order queue;
  staff-entered orders show as "Walk-in". `tests/test_m3_staff_orders.py`
  (48 tests) and a MySQL two-staff same-table race test; CI now expects 9
  MySQL concurrency tests.
- **Account security** (M4, `docs/SECURITY.md` §20): password change at
  `/account/password` (any signed-in user); admin account management at
  `/admin/accounts` (list/filter all accounts, deactivate/activate, issue
  password-reset codes, invite staff); one-time code redemption at
  `/account/setup`. Codes are 256-bit, single-use, expiring and stored only
  as SHA-256 digests. `tests/test_m4_account_security.py` (59 tests) and two
  MySQL races (concurrent code redemption, admins deactivating each other);
  CI now expects 11 MySQL concurrency tests.
- Audit events (M4): `password_changed`, `password_reset_issued`,
  `password_reset`, `staff_invited`, `staff_invitation_accepted`,
  `account_token_rejected`, `account_deactivated`, `account_activated`.
- Settings (M4): `STAFF_INVITE_TTL_HOURS` (default 72) and
  `PASSWORD_RESET_TTL_HOURS` (default 24), each 1–720.
- **Menu & restaurant foundation** (M5, `docs/SECURITY.md` §21):
  - restaurant profile (name, tagline, address, phone, email, default menu
    language), admin-editable at `/admin/settings/restaurant`;
  - display order for categories and menu items;
  - Nepali names and descriptions for categories and menu items, shown on
    the customer menu, with an English/नेपाली switcher (`POST /locale`,
    `?lang=en|ne`) and a vendored Noto Sans Devanagari font;
  - **Menu availability** for STAFF and ADMIN (`/staff/menu`): mark a dish
    sold out or available again.

  `tests/test_m5_catalog.py` (103), `tests/test_m5_migration.py` (1) and a
  MySQL race (two staff marking one dish sold out); CI now expects 12 MySQL
  concurrency tests.
- Audit event (M5): `restaurant_profile_changed`.
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

- **M3:** a dine-in order may only seat a table that is AVAILABLE or
  RESERVED and has no open order; the check runs on a row-locked read inside
  `checkout()`. Seating an OCCUPIED table is now refused for every caller.
- **M4:** staff accounts are created only by an admin's invitation. The
  "Users (staff accounts)" menu entry is now "Staff approvals" (for staff
  accounts that predate M4); "Accounts" is the new management page.
- **M4:** `register_user` no longer accepts `as_staff`; public registration
  creates CUSTOMER accounts only. Account-identity checks shared by
  `create_admin` and invitations moved to `app/services/auth.identity_errors`.
- **M5:** customer-facing pages (header, titles, footer, sign-in) show the
  restaurant profile's name instead of "Quick Junction"; the footer reads
  "Powered by Quick Junction" and the operations console keeps the Quick
  Junction brand. The footer's fixed marketing sentence is replaced by the
  profile's tagline and contact details.
- **M5:** the public menu, home page preview, staff order entry and the
  admin/staff menu lists are ordered by category order, then item order,
  then name, then id (previously by name only). Existing rows default to
  order 0, so the order is unchanged until an admin sets one.
- **M5:** the public-menu rule (available item in an active category) has a
  single implementation, `app/services/menu.public_menu_query`, used by the
  menu, cart, checkout and recommendations. Behaviour is unchanged.

### Removed

- **M2:** `/account/me`, `/account/staff`, `/account/admin` probe endpoints.
- **M4:** public staff sign-up, `/register/staff` (now 404). The
  `staff_registered` event type stays in the allow-list for existing rows.

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
- **M4 — account security:** password change, deactivation, reset issue and
  reset/invitation redemption all revoke existing sessions through
  `session_version`; deactivation also voids open codes. Admins never choose
  or see another user's password. The last active admin cannot be
  deactivated (row-locked check). No password, hash or code is written to
  the audit log or application logs.
- **M5:** restaurant settings are ADMIN-only and audited old -> new in the
  same transaction; staff can change only a dish's availability, through a
  single conditional UPDATE; the language switcher accepts only `en`/`ne`
  and only a local return path (no open redirect); translated text is
  autoescaped like all content.

### Fixed

- `README.md`: the `scripts\reset_database.bat` path was corrupted (a
  carriage-return byte had replaced `\r`).

### Migration

- `a7c2e9d4b1f3` (revises `f3a6d9b2e8c5`): widens the `ck_audit_logs_event_type`
  CHECK constraint to allow `admin_created`. No table or column change; quick on
  any database size. Reversible: `downgrade()` deletes `admin_created` audit rows
  (the admin accounts themselves are kept) and restores the previous list.
- `e8b4d2a6c913` (M4, revises `a7c2e9d4b1f3`): creates `account_tokens`
  and widens `ck_audit_logs_event_type` by the eight M4 events. No existing
  row changes; quick on any database size. Reversible: `downgrade()` deletes
  audit rows of the M4 types and drops `account_tokens` (outstanding codes are
  lost; accounts created by invitation are kept).
- `b1c4e7f9a2d5` (M5, revises `e8b4d2a6c913`): creates `restaurant_profile`
  (one row, seeded as "Quick Junction Demo Restaurant", English) and widens
  `ck_audit_logs_event_type` by `restaurant_profile_changed`. Reversible:
  `downgrade()` deletes those audit rows and drops the table.
- `c2d5f8a1b3e6` (M5, revises `b1c4e7f9a2d5`): adds `sort_order` (default 0,
  CHECK 0–10000) to `categories` and `menu_items` and creates
  `category_translations` and `menu_item_translations` (empty). No existing
  value changes; quick on any database size. Reversible: `downgrade()` drops
  the translation tables (their content is lost) and the columns.

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
- **M2/M3:** staff take dine-in and takeaway orders at **Orders → New order**
  (`/staff/orders/new`), not `/checkout`.
- **M4:** run `flask db upgrade` (new head `e8b4d2a6c913`). Until it is
  applied, every account-management action fails and changes nothing.
- **M4:** new staff no longer sign up. An admin invites them at **Accounts →
  Invite staff** and gives them the one-time code (there is no email
  delivery); they set their password at `/account/setup`.
- **M5:** run `flask db upgrade` (new head `c2d5f8a1b3e6`). Then set the
  real restaurant name and contact details at **Restaurant** in the admin
  sidebar; until then customers see "Quick Junction Demo Restaurant".
- **M5:** Nepali menu text is entered by hand on each category and menu item
  (nothing is translated automatically). Staff mark dishes sold out at
  **Menu availability**.

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
