# Production Readiness — Go-Live Checklist

This is the **gate** a Quick Junction installation must pass before a real
restaurant uses it. It is not a roadmap: it lists what must be true, the current
status, and the evidence required to mark an item READY.

**Rules**

- An item is **READY** only with evidence that can be re-checked (a test, a
  command output, a document that was followed end to end). Code existing is
  not evidence.
- **Go-live requires every item to be READY**, or explicitly accepted as a
  documented exception signed off by the project owner.
- Status values: **NOT STARTED** · **IN PROGRESS** · **READY** · **BLOCKED**
  (cannot start until another item is done — the blocker is named).
- Scope: one restaurant per installation. "1,000 customers" means 1,000
  registered customer accounts, **not** 1,000 concurrent users.

Last reviewed: **2026-10-04**, release **v0.9.0** (audited prototype baseline) plus unreleased M1 (`flask create-admin`), M2 (Phase 1A security hardening), M3 (staff order entry) and M4 (account security and staff onboarding, reviewed 2026-10-08).
Overall: **NOT READY FOR PRODUCTION.**

---

## 1. Security

| # | Item | Status | Current state (evidence) | Required for READY |
| --- | --- | --- | --- | --- |
| S1 | Rate limiting | IN PROGRESS | Login only; in-process dict per worker (`app/utils/ratelimit.py`), keyed on `remote_addr`. M2: `ProxyFix` behind explicit `TRUSTED_PROXY_COUNT` (default 0), spoofed `X-Forwarded-For` tests (`tests/test_m2_security.py`) — limiter itself unchanged | Shared-store limiter (Redis) per IP and per account on login, registration, password reset, checkout, reservations, AI explain; `ProxyFix` with a configured hop count; tests incl. spoofed `X-Forwarded-For` |
| S2 | Authentication | READY (application) | Argon2id, session fixation protection, server-side revocation — tested (`tests/test_auth.py`, `tests/test_session_revocation.py`); rehash on login done in M2 (`docs/SECURITY.md` §18.6). M4: self-service password change (revokes other sessions, per-account attempt limit) and admin-issued reset by one-time code — admins never see a password (`tests/test_m4_account_security.py`, `docs/SECURITY.md` §20). No email delivery: codes are handed over in person | Password change; admin-issued reset; rehash on login; tests |
| S3 | Account management | READY (application) | M4: `/admin/accounts` (ADMIN only) lists all accounts with a role filter, deactivates/reactivates with session revocation and code voiding, last-active-admin protection under a row lock (MySQL race test); audited in the same transaction. No search box yet | Admin list/search/deactivate/reactivate with session revocation; last-admin protection; audited; tests |
| S4 | Staff onboarding security | READY (application) | M4: `/register/staff` removed; staff accounts only by admin invitation — 256-bit single-use expiring code stored as a SHA-256 digest, account inactive until the code is redeemed (`docs/SECURITY.md` §20.3–20.4) | Invitation-only staff accounts; public staff sign-up removed |
| S5 | Authorization | READY (application) | `require_role` on every non-public route; owner-scoped customer queries; tests across role boundaries | Re-verify after each Phase 1 change |
| S6 | Dine-in abuse protection | READY (application) | M2: customer checkout is CUSTOMER-only and takeaway/online only; any `table_id` refused server-side; crafted-request tests incl. RESERVED and unknown tables (`tests/test_m2_security.py`, `docs/SECURITY.md` §18.2). M3: dine-in is created only through staff order entry (STAFF/ADMIN), which seats a table under a row lock; MySQL two-staff race test (`docs/SECURITY.md` §19) | Customer checkout cannot occupy tables; tests |
| S7 | Reservation abuse protection | READY (application) | M2: `RESERVATION_ACTIVE_LIMIT` (default 2 active future CONFIRMED) under a `users`-row lock; MySQL race test passes 15/15 rounds × 5 runs and fails without the lock (`docs/SECURITY.md` §18.3) | Per-account cap enforced under concurrency (MySQL test) |
| S8 | Secret management | NOT STARTED | `.env` file; production location and rotation undocumented | Secrets in a root-owned 0600 file outside the code tree; rotation procedure documented; no real credentials in comments |
| S9 | Audit integrity | IN PROGRESS | M2: business change and audit row commit in one transaction (`commit_with`), failure-injection tests; independent auth events commit separately (`docs/SECURITY.md` §18.4). Retention job not started | Same-transaction audit for business events; retention job |
| S10 | Database privilege separation | NOT STARTED | One account holds DML **and** DDL (`scripts/db_setup.py`) | Runtime account DML-only; separate migration account |
| S11 | Security headers | IN PROGRESS | CSP (no inline script), `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `no-store` — tested; HSTS only when cookies are Secure | Verified on the deployed HTTPS stack; `Server` version header hidden |
| S12 | CSRF | READY (application) | `CSRFProtect` app-wide; all state changes are POST forms; tested | Re-verify behind the reverse proxy |
| S13 | Password hashing | READY (application) | Argon2id library defaults (`app/utils/security.py`); rehash on successful login (M2) | Re-verify on the production stack |
| S14 | Dependency vulnerability scanning | IN PROGRESS | `pip-audit` in CI (M0): runtime clean; AI stack has 13 known advisories, re-checked in M2; AI stack now disabled by default and not needed by the core app (`docs/SECURITY.md` §18.9) | CI green on GitHub; AI stack disabled in production or advisories resolved; hash-locked requirements |
| S15 | Remove unnecessary endpoints | READY (application) | M2: probe routes removed; tests use a test-only blueprint; 404 asserted (`tests/test_m2_security.py`) | Re-verify the route list on each release |

## 2. Deployment

| # | Item | Status | Current state (evidence) | Required for READY |
| --- | --- | --- | --- | --- |
| D1 | Linux production environment | NOT STARTED | Windows development scripts only | Documented supported OS + Python 3.11 build; installed from docs on a clean VM |
| D2 | Nginx | NOT STARTED | — | Reverse proxy config; static files; body-size limit; forwarded headers matching `ProxyFix` |
| D3 | HTTPS | NOT STARTED | — | Certificate, auto-renewal, HTTP→HTTPS redirect, TLS ≥ 1.2, expiry alert |
| D4 | Gunicorn | NOT STARTED | Documented in `run.py`, **not** in `requirements.txt` | Pinned; config file; worker count from Phase 5 measurements |
| D5 | systemd | NOT STARTED | — | Service unit as a non-login user with hardening; restart on failure; survives reboot |
| D6 | Production configuration | IN PROGRESS | `ProductionConfig` refuses debug, weak `SECRET_KEY`, SQLite, MySQL root; M2: `LLM_ENABLED` defaults to false, `TRUSTED_PROXY_COUNT` defaults to 0, `RESERVATION_ACTIVE_LIMIT` defaults to 2 | Production defaults reviewed (LLM off, log location, secure cookies) and documented |
| D7 | Health / readiness checks | IN PROGRESS | `/health` liveness only | Readiness check covering DB, Redis and migration head; not public |
| D8 | Logging | IN PROGRESS | Rotating `app.log` / `security.log` inside the code directory | Logs outside the code tree, rotated, request IDs |
| D9 | Monitoring | NOT STARTED | — | Uptime check, error tracking, disk/CPU/memory, backup-job and certificate alerts — each alert tested once |
| D10 | Admin bootstrap | READY (application) | M1: `flask create-admin` — interactive only, hidden password prompts, existing username/email/password rules, Argon2id, account + `admin_created` audit row in one transaction; `tests/test_create_admin.py` passes on SQLite and MySQL 8.0 | Re-verify once on the production stack during the first installation (O2) |

## 3. Data protection

| # | Item | Status | Current state (evidence) | Required for READY |
| --- | --- | --- | --- | --- |
| P1 | Automated backups | NOT STARTED | Manual `mysqldump` commands in `docs/DATABASE_SETUP_GUIDE.md` §19 | Scheduled consistent dumps with success/failure alerting |
| P2 | Off-machine backups | NOT STARTED | — | Copied to separate storage with write-only credentials |
| P3 | Encryption | NOT STARTED | — | Backups encrypted; key held off the server, with escrow |
| P4 | Restore testing | NOT STARTED | — | Automated periodic restore into a scratch database with checks; one manual drill timed against the RTO |
| P5 | Disaster recovery procedure | NOT STARTED | — | Written per scenario (disk loss, bad migration, compromise, lost key); RPO/RTO agreed |
| P6 | Retention | NOT STARTED | Audit log and backups kept indefinitely | Retention periods defined and enforced |

## 4. Release management

| # | Item | Status | Current state (evidence) | Required for READY |
| --- | --- | --- | --- | --- |
| R1 | Semantic version | IN PROGRESS | `v0.9.0` tag (M0) | Version in code, shown to admins and in a restricted version endpoint; CI checks tag = code version |
| R2 | CHANGELOG | IN PROGRESS | `CHANGELOG.md` with Migration / Operator-action sections (M0) | Enforced on every release tag |
| R3 | Migrations | IN PROGRESS | 11 linear revisions, head `f3a6d9b2e8c5`; upgrade tested on SQLite and MySQL in CI; full downgrade fails on MySQL (#32) | Every new migration has a MySQL upgrade/downgrade test or is marked irreversible; separate migration account |
| R4 | Upgrade procedure | NOT STARTED | — | One-command upgrade with mandatory pre-upgrade backup, rehearsed on staging |
| R5 | Rollback procedure | BLOCKED (by P1, R4) | — | Scripted rollback (code-only and with-migration paths), rehearsed |
| R6 | Release artifact | NOT STARTED | Source checkout; transitive dependencies unpinned | Tagged artifact with hash-locked requirements; model files distributed with checksums |
| R7 | CI on protected `main` | IN PROGRESS | Workflow added (M0); branch protection **not verified** | CI green on GitHub; branch protection enabled and confirmed in repository settings |

## 5. Capacity

No capacity statement may be published until these are measured.

| # | Item | Status | Current state (evidence) | Required for READY |
| --- | --- | --- | --- | --- |
| C1 | Measured load test | BLOCKED (by D1–D5) | None performed | Load tests on the production-like stack on the recommended hardware; results published in `docs/CAPACITY.md` |
| C2 | Registered-user capacity | NOT STARTED | UNKNOWN | ≥ 1,000 customer accounts with realistic order history (e.g. tens of thousands of orders) without degrading the SLOs |
| C3 | Concurrent-user capacity | NOT STARTED | UNKNOWN | Measured peak concurrent active users within agreed SLOs |
| C4 | Checkout throughput | NOT STARTED | UNKNOWN; correctness under races verified by 7 MySQL tests | Measured checkouts/minute at the agreed peak with no invariant violations |
| C5 | Resource measurements | NOT STARTED | UNKNOWN | RAM per worker (with/without LLM), CPU, DB connections, backup duration and size |
| C6 | Unbounded queries removed | NOT STARTED | Staff queue, customer history, payments/audit views unpaginated | Pagination + indexes, verified with `EXPLAIN` on a large dataset |

## 6. Pilot operations

| # | Item | Status | Current state (evidence) | Required for READY |
| --- | --- | --- | --- | --- |
| O1 | Restaurant configuration | NOT STARTED | Slots, currency, branding are code constants | Admin-editable settings |
| O2 | Admin bootstrap | BLOCKED (by D1) | Command exists (D10); no installation yet. M4: the owner can change the operator-chosen password at `/account/password` | Owner account created on the installation with `flask create-admin`; owner able to change the password (S2) |
| O3 | Staff onboarding | NOT STARTED | M4: invitation workflow exists (S4); no installation yet | Staff invited and able to sign in |
| O4 | Menu setup | IN PROGRESS | Admin menu/category/ingredient/recipe/table management exists and is tested | Restaurant's real menu, tables and tax rate entered and reviewed |
| O5 | Backup verification | BLOCKED (by P1–P4) | — | First backup restored successfully before go-live |
| O6 | Monitoring | BLOCKED (by D9) | — | Alerts route to the support contact |
| O7 | Smoke test | NOT STARTED | Test-client end-to-end flows exist (`tests/test_demo_flow.py`) | Scripted smoke test against the live installation: order → kitchen → payment → report |
| O8 | Support runbook | NOT STARTED | — | Common incidents with exact steps; remote-access policy |
| O9 | Staff training | NOT STARTED | — | Admin, staff and kitchen guides; training session held |
| O10 | Product gaps for pilot | IN PROGRESS | M3: staff order entry for dine-in/takeaway (`/staff/orders/new`). Still missing: order notes, receipts, kitchen auto-refresh, adding items to an open order. Delivery is already closed to customers (M2) | Pilot-critical items (roadmap Phase 6a) done |
| O11 | Go-live sign-off | NOT STARTED | — | Every item above READY or a signed exception; rollback plan agreed |
