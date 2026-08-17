# Security — Milestone 03 baseline

Scope: foundation (M01), auth/authorization/CSRF/audit core (M02), plus the
menu data layer and menu-management surface (M03). No cart, checkout,
order, or payment feature exists yet. This document records what is in
place, **why**, and what must be added before Quick Junction handles real
orders.

---

## 1. Secret management

**In place**

- Every secret and credential is read from the process environment:
  `SECRET_KEY`, `DATABASE_URL`. Nothing else in the tree holds one.
- `config.py` contains variable *names* and non-sensitive defaults only.
- `.env` is loaded by `python-dotenv` if present and is git-ignored. The
  repository ships `.env.example` with placeholders and never a real `.env`.
- `ProductionConfig.validate()` refuses to start when `SECRET_KEY` is
  missing, shorter than 32 characters, or one of the known placeholder
  strings (`change-me`, the testing placeholder, the `.env.example` value…).
- The testing configuration uses a fixed, obviously-fake secret
  (`testing-only-not-a-secret`). It is the only credential literal in the
  tree and it cannot satisfy production — a test asserts exactly that.

**Why:** a secret in source is a secret in every clone, every fork, every CI
log and every backup, forever. Environment variables scope it to one host.

**Rules**

- Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
- A committed secret is a compromised secret: **rotate first**, clean history
  second.
- Never log a credential. Log identifiers.

---

## 2. Password hashing

**Algorithm: Argon2id**, via `argon2-cffi` (`app/utils/security.py`), using
the library's own default parameters (time cost, memory cost, parallelism)
rather than hand-picked ones — the maintained default already targets the
OWASP-recommended range and improves as the library's defaults improve.
Hand-rolling parameters is how projects end up under-salted or
under-iterated.

- `hash_password()` / `verify_password()` wrap `argon2.PasswordHasher`.
- `needs_rehash()` is exposed for a future migration path when parameters
  change; not called automatically yet (no login-time rehash) — see §10.
- `password_hash` is `VARCHAR(255)`: an encoded Argon2id hash is ~100
  chars; the headroom absorbs a parameter change without a migration.
- The hash is **never** included in any JSON response, template, or log
  line — test 22 in `tests/test_auth.py` asserts this against a live
  response.

**Password policy** (`password_policy_errors`, enforced server-side in
`register_user`): 10–128 characters, at least one letter and one digit, not
in a small deny-list of extremely common passwords. Enforced server-side
regardless of what client-side script does or skips.

**Why 10 chars + a tiny deny-list instead of forced complexity rules:**
length is the strongest lever against brute force; mandatory
uppercase/symbol rules mostly push users toward predictable substitutions
(`Password1!`). A maintained breach-corpus check (e.g. an HaveIBeenPwned
k-anonymity lookup) is stronger than any hand-typed list and is future work,
not this milestone's — see §10.

---

## 3. Authentication

`app/services/auth.py` (`register_user`, `authenticate_user`) plus
`app/utils/authorization.py` (session helpers) and `app/routes/auth.py`
(`/register`, `/login`, `/logout`).

- **Session-based**, using Flask's signed cookie session — no server-side
  session store yet. The session holds exactly one value: the user's
  integer id. Flask signs it with `SECRET_KEY` (tamper-evident) but does
  **not** encrypt it, so nothing beyond an id belongs there.
- **Session fixation:** `login_user()` calls `session.clear()` before
  writing the new session, so any pre-authentication session id is
  discarded rather than reused post-login.
- **Every request re-loads the user from the database**
  (`get_current_user()`), not from the session payload. A role change or
  `is_active = False` takes effect on the user's very next request, not
  after their session happens to expire.
- **Registration** always creates `role = CUSTOMER` server-side; the
  registration form has no role field, so there is no request parameter
  that could set one (test 14). Username/email uniqueness is checked
  before insert for a friendly error, and the database's `UNIQUE`
  constraint is the real backstop against the check-then-insert race
  (caught as `IntegrityError`, test-covered).
- **Login failure is generic and timing-parity'd**: `authenticate_user`
  returns `None` for an unknown username, a wrong password, or an inactive
  account — the caller cannot distinguish them, and neither can the
  response built from it (test 21). See §7 for the enumeration reasoning.

---

## 4. Authorization

`app/utils/authorization.py`: `login_required` and `require_role(*roles)`.

```python
@account_bp.get("/admin")
@require_role(Role.ADMIN)
def admin_only(): ...
```

- `require_role` implies `login_required`: an anonymous request gets 401,
  an authenticated request with the wrong role gets 403. The distinction is
  meaningful to a legitimate client (log in vs. not permitted) and neither
  response discloses what the endpoint would have returned.
- **Roles: `ADMIN`, `STAFF`, `CUSTOMER`.** Stored as a constrained string
  (`sa.Enum(..., native_enum=False)`, `VARCHAR(16)` with a `CHECK`
  constraint) on `users.role`, not a separate table — three fixed roles
  with no per-role attributes don't earn a join. Default is `CUSTOMER`.
- **Authorization is server-side only.** The role decision reads
  `User.role` from the database on every request; nothing in a cookie,
  header, or form field can substitute for it (test 15 sends a forged
  `X-Role` header and confirms it is ignored).
- **`app/routes/account.py` is authorization-foundation, not a feature**:
  `/account/`, `/account/me`, `/account/staff`, `/account/admin` exist so
  the decorators have something real to protect and so the manual/browser
  test has something to click. Later milestones replace them with actual
  staff and admin views.

**Menu management role matrix (M03)**, all enforced by the same
`require_role`, no separate mechanism:

| Route | CUSTOMER | STAFF | ADMIN |
| --- | --- | --- | --- |
| `GET /menu`, `GET /menu/<id>` | ✅ (and anonymous) | ✅ | ✅ |
| `GET /admin/menu` (view all, including unavailable) | 403 | ✅ | ✅ |
| `GET/POST /admin/menu/create`, `/admin/menu/<id>/edit` | 403 | 403 | ✅ |
| `GET /admin/categories` and all category routes | 401/403 | 403 | ✅ |

Every cell above is a live test in `tests/test_menu.py` (tests 10-14) and
was independently re-verified against the running dev server in a real
browser as three separate logged-in sessions (customer, staff, admin) —
not just asserted by the test suite. A customer or an anonymous visitor
gets 401 (not authenticated) or 403 (authenticated, wrong role) — never a
200 with a stripped-down response; the endpoint does not exist for them,
full stop.

---

## 5. CSRF protection

**Implemented this milestone**, per the plan in the Milestone 01 baseline.

- `Flask-WTF`'s `CSRFProtect` is initialized on every request
  (`app/extensions.py`, wired in `app/__init__.py`). `WTF_CSRF_ENABLED` is
  `True` in development and production, `False` only in the testing
  config (individual tests flip it back on to exercise CSRF directly —
  see `tests/test_auth.py` tests 16–18).
- Every state-changing form (`register.html`, `login.html`, `account.html`'s
  logout form) carries a hidden `csrf_token` field.
- `GET` handlers stay side-effect free, which is what CSRF's cookie/token
  double-submit defense assumes.
- A missing or forged token is rejected with `400` before the view function
  runs at all — verified against a live server with a raw `fetch()` with no
  token and with a fabricated `X-CSRFToken` header (see the manual test
  report), and by tests 16/17.
- `SameSite=Lax` on the session cookie remains active as defense in depth.
- No JSON API exists yet that would need a per-route CSRF exemption; when
  one is added, it is exempted explicitly, per route, never globally.

---

## 6. Session security

| Setting | Value | Why |
| --- | --- | --- |
| `SESSION_COOKIE_HTTPONLY` | `True` | JavaScript cannot read the cookie, so an XSS bug cannot trivially steal a session. |
| `SESSION_COOKIE_SAMESITE` | `Lax` | Blocks the cookie on cross-site POSTs — defence in depth behind CSRF tokens. |
| `SESSION_COOKIE_SECURE` | `True` (`False` in development/testing) | The cookie is never sent over plain HTTP. Relaxed only for `localhost`, where TLS is absent. |
| `SESSION_COOKIE_NAME` | `qj_session` | Does not advertise the framework. |
| `PERMANENT_SESSION_LIFETIME` | 8 hours | A shift-length session; a forgotten terminal stops being a permanent door. |
| `MAX_CONTENT_LENGTH` | 2 MiB | Caps request bodies so a large upload cannot exhaust memory. |

`login_user()` sets `session.permanent = True` so the 8-hour lifetime
applies; `logout_user()` calls `session.clear()`, which both removes the
user id and rotates the cookie's signature, invalidating the old session
immediately (test 20: a protected endpoint returns 401 right after logout,
in the same test client / cookie jar).

---

## 7. User enumeration considerations

Two different surfaces, two different answers:

- **Login** (`/login`): the error is always the literal string *"Invalid
  username or password."* — identical whether the username doesn't exist,
  the password is wrong, or the account is deactivated. `authenticate_user`
  additionally runs a real Argon2 verification against a fixed dummy hash
  when no account matches, so the unknown-username path does roughly the
  same amount of work as a real mismatch instead of returning early and
  leaking existence through timing.
- **Registration** (`/register`): duplicate username and duplicate email
  **are** reported specifically ("That username is already taken." /
  "That email is already registered."). This is normal signup UX, not the
  same bug class as login enumeration — a user attempting to register a
  given identifier has already asserted an interest in it, so confirming
  its existence discloses nothing they didn't already probe for by
  submitting the form. Every mainstream signup flow makes this same
  trade-off.

---

## 8. Rate limiting — decision

**Implemented as a small in-memory limiter** (`app/utils/ratelimit.py`),
not `Flask-Limiter`: 5 failed attempts per `(client IP, normalized
username)` pair per 15-minute window; the 6th attempt gets `429` before
credentials are even checked, and the event is audit-logged as
`login_rate_limited`. A successful login resets the counter for that key.

**ponytail-flagged ceiling:** the limiter is a `dict` behind a
`threading.Lock`, in-process only. It resets on restart and does not share
state across `gunicorn` workers or multiple instances. That is real
protection for a single dev/staging process and is not sufficient for a
multi-worker production deployment.

**Why not add `Flask-Limiter` now:** it is a new dependency this milestone
does not need to prove the concept, and it still needs a shared backend
(Redis, in practice) to work correctly across workers — which is itself a
new piece of infrastructure. `_LoginAttemptLimiter` exposes exactly three
methods (`is_limited`, `record_failure`, `reset`); swapping in a
Redis-backed `Flask-Limiter` call is a drop-in replacement behind that same
interface when the deployment target needs it (see §10).

Login is the only endpoint rate-limited this milestone — it is the only
endpoint that authenticates against a secret. Public endpoints in general
are noted as a follow-up in §10.

---

## 9. Input-validation strategy

**Now enforced on the auth routes**, per the rule set from the Milestone 01
baseline (still the standing rule for every future route):

1. **Validate at the boundary.** `app/routes/forms.py` (WTForms:
   `RegistrationForm`, `LoginForm`) validates and coerces before
   `app/services/auth.py` is ever called. Services re-validate policy
   (password strength) independently, since a service must be safe to call
   from anywhere, not just from these forms.
2. **Allow-list, never deny-list.** Username: `^[a-zA-Z0-9_]{3,32}$`.
   Email: WTForms' `Email()` validator (via `email-validator`).
3. **Type, range, and length** on every field — see the `Length` validators
   in `forms.py` and the column widths in `app/models/user.py`.
4. **Never build SQL by string concatenation.** Every query in this
   codebase goes through the SQLAlchemy ORM.
5. **Escape on output, not on input.** Jinja2 autoescaping stays on; no
   template uses `|safe`.
6. **Money is `Decimal`, never `float`, end to end.** `MenuItem.price` is
   `Numeric(10, 2)`, parsed and range-checked in
   `app/services/menu.py::_validate_price` (rejects non-numeric input,
   `<= 0`, and an absurd upper bound), then backed by a database
   `CHECK (price > 0)` as well — three independent layers, not one.
   `tests/test_menu.py::test_9` proves no float rounding drift survives a
   round trip through the database.
7. **Reject, log, and return a generic message.** A failed registration
   or menu-management form re-renders with field-specific errors (safe:
   those are static, known strings, not echoed attacker input). A failed
   login gets the one generic sentence from §7.
8. **Trust nothing from the client for authorization** — role, a price, a
   total, or an id is always re-checked against the database (see §4).
   `GET /menu/<id>` ignores every query parameter; there is no code path
   that reads a price or an availability flag from a request
   (`tests/test_menu.py::test_17` submits a spoofed `?price=` and confirms
   it has zero effect on the rendered or stored price).
9. **Allow-list enumerated fields at the database, not just the form.**
   `cuisine`, `spice_level`, `dietary_type` are Python enums
   (`app/models/enums.py`) rendered as a `<select>` *and* backed by a
   database `CHECK` constraint — see the "Allow-listed enum columns"
   writeup in `docs/DATABASE.md`, including a bug this milestone caught
   and fixed (the constraint was silently absent before
   `create_constraint=True` was added). `tests/test_menu.py::test_7b`
   proves the database itself rejects an invalid value via a raw `INSERT`
   that bypasses both the service layer and SQLAlchemy's own Python-side
   validation.

`MAX_CONTENT_LENGTH` (2 MiB) continues to bound request size.

---

## 10. Audit logging

`app/models/audit_log.py` (`AuditLog`, `AuditEvent`) +
`app/services/audit.py` (`record_event` — the only writer).

**Recorded events:** `register_success`, `login_success`, `login_failure`,
`login_rate_limited`, `logout` (M02); `category_created`,
`category_updated`, `category_deactivated`, `menu_item_created`,
`menu_item_updated`, `menu_item_availability_changed`,
`ingredient_changed` (M03).

**`user_id` means "actor" for the M03 events**, not "subject" — it is the
id of the admin who performed the action, not any user the menu item might
somehow relate to (menu items have no owner). Auth events keep the M02
meaning (the account the event is about). Both readings coexist without a
schema change because the column has always meant "the account most
relevant to this row," and for a menu edit that is the person who edited
it.

**Columns, deliberately narrow:** `event_type`, `user_id` (nullable — a
failed login for a username that doesn't exist names no account),
`success`, `ip_address`, `user_agent` (truncated to 255 chars),
`metadata_json` (small, non-sensitive context — the attempted username on
a login failure, or the affected `category_id`/`menu_item_id` and name on
a menu-management event, capped at 500 chars), `created_at`.

**Granularity is deliberate**, not just "logged, generically": editing a
menu item's availability specifically produces
`menu_item_availability_changed` rather than a generic `menu_item_updated`
(the route compares the pre-call and post-call state to pick the event —
see `docs/ARCHITECTURE.md`'s Menu management section), and a single form
submission can produce more than one row (e.g. creating a menu item with
ingredients logs both `menu_item_created` and `ingredient_changed`).
`tests/test_menu.py::test_22` asserts on this exactly, and it was
independently confirmed by reading `logs/security.log` after the
milestone's manual browser walkthrough.

**Never recorded — enforced in code, not just by convention:**
`record_event` raises `ValueError` if the caller's `metadata` dict contains
any of `password`, `password_hash`, `token`, `session`, `secret`, `cookie`
— a fail-loud guard against a future call site accidentally passing one of
these. Test 28 asserts no attempted password appears anywhere in a
populated audit table.

**Two channels, not one:** every event is written to the `audit_logs` table
*and* mirrored to the `quickjunction.security` logger (`logs/security.log`
— see the Milestone 01 baseline's §7, renumbered here). The log line is
intentionally smaller than the database row (event, user id, IP — no
metadata), so a compromised or temporarily-unavailable database is not the
only way to lose visibility into authentication activity.

**Data minimization:** no name, phone number, or physical address is
collected or logged anywhere in this milestone — the `users` table has
exactly `username`, `email`, `password_hash`, `role`, `is_active`, and
timestamps.

**Retention:** not enforced in code. `audit_logs` rows contain IP addresses
and login activity and should be treated as sensitive: a defined retention
window (90 days is a reasonable starting point) and access restricted to
operators. Automatic purging is future work — see §12.

---

## 11. Debug and environment separation

- `DEBUG` is `True` only in `DevelopmentConfig`.
- `ProductionConfig` hard-codes `DEBUG = False` / `TESTING = False` and its
  `validate()` raises if either is somehow true.
- `run.py` passes `debug=app.config["DEBUG"]` — it never hard-codes `True`.
- Production is served by a WSGI server (`gunicorn "app:create_app('production')"`),
  never by `run.py`.

**Why:** the Werkzeug debugger exposes an interactive Python console on any
traceback. Debug mode in production is remote code execution.

---

## 12. Error handling

`app/utils/errors.py` registers two handlers:

- `HTTPException` (including Flask-WTF's `CSRFError`, which subclasses
  `BadRequest`) → the Werkzeug status and its generic description.
- `Exception` → logged with a full traceback server-side, and returned to
  the client as `{"error": {"status": 500, "message": "An internal error occurred."}}`.

`PROPAGATE_EXCEPTIONS = False` in every configuration, so an unexpected
error — including a database error such as a lost connection — can never
reach the client as a traceback or a driver message, not even under
`TESTING` (tests 23/24 assert this against a deliberately-raising route and
a mocked `OperationalError` on commit, respectively).

**Known simplification:** the error handlers always return JSON, even for
a browser hitting an HTML form (e.g. an unexpected 500 on `/register`). No
content negotiation exists yet. Acceptable for a foundation milestone with
two HTML pages total; revisit when the frontend milestone adds real pages
users are expected to read errors on.

---

## 13. Security logging

`app/utils/logging.py` provides two channels:

- `app.logger` → console + `logs/app.log` (rotating, 2 MiB × 5).
- `quickjunction.security` → console + `logs/security.log` (rotating,
  always at least `INFO`, non-propagating) — now actively written to by
  every audit event (§10).

`logs/` is git-ignored. File logging is off in the testing configuration.

**What it must never record:** passwords, session tokens, API keys, full
database URIs, card data, or any full request body. Verified by test 28
and by manual inspection of `logs/security.log` and `logs/app.log` during
the milestone's browser walkthrough.

*Known gap, carried forward:* Alembic's `env.py` renders the database URL
with the password visible when it sets `sqlalchemy.url`. Keep Alembic's
log level at `WARN` (the generated default) and never paste migration
output into a ticket.

---

## 14. Health endpoint

`GET /health` returns exactly `{"status": "ok"}`. Unchanged from Milestone
01: no version, environment name, hostname, path, configuration value, or
database status.

---

## 15. Protection against accidental secret commits

Unchanged from Milestone 01 — `.gitignore` coverage, no real `.env` in the
repository. See the project `README.md` for the current rule set.

---

## 16. Not yet implemented — required before production

Ranked by how much damage the absence causes. Items completed this
milestone (authentication, authorization, CSRF, session fixation
protection, an initial rate limiter) are removed from this list.

| # | Item | Needed by |
| --- | --- | --- |
| 1 | Multi-worker-safe rate limiting (Flask-Limiter + Redis) | first multi-worker/multi-instance deployment |
| 2 | Security headers: HSTS, `X-Content-Type-Options`, `X-Frame-Options`, CSP (via Flask-Talisman or the reverse proxy) | first HTML page shipped to real users |
| 3 | Strip the `Server` header — the dev server advertises `Werkzeug/... Python/...` | deployment |
| 4 | TLS termination + HTTP→HTTPS redirect | deployment |
| 5 | A dedicated MySQL user with least privilege — no `GRANT ALL` in production | deployment |
| 6 | Password reset flow (with its own rate limiting and token expiry) | first locked-out real user |
| 7 | Email verification at registration | before email is trusted for anything (e.g. password reset) |
| 8 | Login-time rehash (`needs_rehash()` is written but not called) | first Argon2 parameter change |
| 9 | A maintained breached-password check (e.g. HaveIBeenPwned k-anonymity) replacing the hand-typed deny-list | before it matters for a real user base |
| 10 | Audit log retention/purge policy, enforced in code | first real data / compliance review |
| 11 | Audit trail for order/billing changes | first order |
| 12 | Encrypted, tested database backups | first real data |
| 13 | Dependency scanning (`pip-audit`) in CI | first CI run |
| 14 | Content negotiation on error responses (HTML vs JSON) | first real HTML page beyond auth forms |
| 15 | **MySQL verification of the M02+M03 migration** (`CHECK` constraints, `Numeric` price type, foreign keys, indexes) — no local MySQL server was reachable in this environment; see `docs/DATABASE.md` | before any real (non-SQLite) deployment |
| 16 | Ingredient administration beyond the menu-item form's comma-separated field (rename/merge/delete an ingredient independently of any one menu item) | first time a typo'd ingredient name needs correcting across many items |
| 17 | Per-field price-change audit history (currently a price change is folded into the generic `menu_item_updated` event, not its own event) | if price-change history becomes a compliance or dispute-resolution need |

### AI-specific, for the milestone that adds the model

- **Prompt injection is an input-validation problem.** Model output is
  untrusted text: it may never be executed, never be interpolated into SQL,
  and never be rendered unescaped.
- The model must not receive customer PII. Send menu items and preferences,
  not names, phone numbers or addresses.
- The model advises; it never authorises. Prices, discounts and totals are
  computed server-side and never taken from model output.
- Local inference only. No external AI API — that is a project requirement
  and a data-residency guarantee, not a preference.
- Model weights and LoRA adapters are treated as untrusted binaries: load
  `safetensors`, pin the source, never `pickle`.
