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

### Fixed

- `README.md`: the `scripts\reset_database.bat` path was corrupted (a
  carriage-return byte had replaced `\r`).

No application code, configuration, dependency or migration changed.

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
