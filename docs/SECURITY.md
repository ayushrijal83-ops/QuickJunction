# Security — Milestone 04 baseline

Scope: foundation (M01), auth/authorization/CSRF/audit core (M02), the menu
data layer and menu-management surface (M03), plus cart, checkout, order
placement, and order history (M04). No payment gateway exists yet. This
document records what is in place, **why**, and what must be added before
Quick Junction handles real payments.

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
  change; not called automatically yet (no login-time rehash) — see §17.
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
not this milestone's — see §17.

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
interface when the deployment target needs it (see §17).

Login is the only endpoint rate-limited this milestone — it is the only
endpoint that authenticates against a secret. Public endpoints in general
are noted as a follow-up in §17.

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

## 10. Cart, checkout, and order trust boundary (M04)

The single rule this whole milestone exists to enforce: **a price, a
subtotal, or a total is never accepted from a client.** The client submits
only a menu item id and a quantity; every monetary value that ends up on an
`Order` or `OrderItem` is computed server-side, from the database, inside
`app/services/orders.py::checkout`.

**What the cart is (and is not).** `app/utils/cart.py` stores exactly
`{menu_item_id: quantity}` in Flask's signed session cookie — no price, no
item name, no line total, no subtotal. Because the session is
`itsdangerous`-signed with `SECRET_KEY`, a client can read but not alter
its contents without invalidating the signature; a tampered cookie is
rejected by Flask before a request reaches a view. That is what makes a
session cart safe to use here without a database table or a new dependency
— but it is *defense in depth*, not the reason prices are safe. Even a
cart that were fully attacker-controlled could not inject a price, because
nothing about price is ever read from it.

**Server-side revalidation happens on every read, not just at checkout.**
`app/services/cart.py::build_cart_view` re-resolves every cart line against
the live `MenuItem` table (same "active category + available item" filter
as the public menu, `app/services/menu.py::get_public_menu_item`) on every
`GET /cart` — a price change or a deactivation between "add to cart" and
"view cart" is visible immediately.

**Checkout order of operations** (`app/services/orders.py::checkout`),
every step server-side, none trusting the caller:

1. Parse the session cart's `{menu_item_id: quantity}` pairs.
2. Reload each menu item from the database.
3. Verify each is still available (active category + `is_available`).
4. Verify each quantity is in bounds (1–20 per item, ≤30 distinct items).
5. Recompute every unit price and line total from the *current* `MenuItem.price`.
6. Sum the subtotal and total.
7. Write `Order` + its `OrderItem` rows in one transaction.

Any failure at any step raises `CheckoutError` before a single row is
written, or triggers `db.session.rollback()` if a step after the write has
already begun (test 19, `tests/test_orders.py`, forces a commit failure via
monkeypatch and asserts zero `Order`/`OrderItem` rows survive). There is no
code path that creates a partial order.

**Never accepted as authoritative — verified by test, not just by
inspection:** a client-submitted `unit_price`, `subtotal`, or `total` field
on the checkout POST is not merely validated and rejected — there is no
form field, no service parameter, and no code path that reads one at all.
`tests/test_orders.py` tests 15–17 submit exactly these fields alongside a
real checkout and assert the stored order matches the server-computed
value regardless. The manual browser walkthrough went further: injecting
hidden `total`, `subtotal`, `unit_price`, and `status` fields into the
checkout form via the DevTools console before submitting, and confirming
the resulting order's price and status were unaffected (see the final
report).

**Price snapshots freeze history.** `OrderItem.item_name_snapshot` and
`unit_price_snapshot` are copied from `MenuItem` at checkout time and never
updated again — a later rename or price change on the live menu item must
never rewrite what a customer was actually charged (test 18 changes both
after checkout and confirms the stored order line is untouched). This is
also why `Order`/`OrderItem` have their own columns rather than a foreign
key computation: an order is a historical record, not a live view onto the
menu.

**Order status cannot be set or changed by a customer.** `checkout` always
writes `OrderStatus.PENDING`; there is no form field, service parameter, or
route that accepts a client-supplied status, at creation or afterward (test
22 submits a `status=completed` field on the checkout POST and confirms the
created order is still `PENDING`, then confirms no route exists for a
customer to change it post-creation). Staff status management is deferred
to Milestone 05 — see `docs/ARCHITECTURE.md`.

**Ownership (IDOR) is enforced in the query, not after fetching the row.**
`app/services/orders.py::get_order_for_user` and `list_orders_for_user`
both filter by `user_id` as part of the SQL query itself — a mismatched
order id and a non-existent order id are indistinguishable, both returning
`None`/404. `GET /orders/<id>` never returns 403 for someone else's order;
403 would itself confirm the id exists. Test 21
(`tests/test_orders.py::test_21_idor_attempt_rejected`) logs in as a second
account and confirms a direct request for the first account's order id
returns a plain 404, and the manual browser walkthrough repeated the same
attack by hand (see the final report).

**Checkout requires authentication; the cart does not.** `GET/POST
/checkout`, `GET /orders`, and `GET /orders/<id>` are all
`@login_required` (test 11 confirms both the checkout page and the
checkout POST return 401 for an anonymous request, and that no order is
created). `GET /cart` and the four cart-mutation routes are intentionally
open to anonymous visitors — a session cart holds no PII and no money
moves until checkout, so there is nothing an anonymous cart exposes that
requires gating it behind login.

**CSRF, same mechanism as every other state-changing form in this app.**
Every cart-mutation route and the checkout POST are `FlaskForm`s carrying
a `csrf_token` field, checked by Flask-WTF's `CSRFProtect` before the view
body runs (test 23, plus a live `fetch()` with no token against the running
dev server during the manual walkthrough, confirming `400`).

**What is out of scope for this milestone, by design:** no payment
gateway, no card data of any kind, no tax/discount/delivery-fee
calculation (`total` equals `subtotal` unconditionally), no order
cancellation, and no staff-facing status change. See
`docs/ARCHITECTURE.md`'s "Room reserved for later work."

---

## 11. Audit logging

`app/models/audit_log.py` (`AuditLog`, `AuditEvent`) +
`app/services/audit.py` (`record_event` — the only writer).

**Recorded events:** `register_success`, `login_success`, `login_failure`,
`login_rate_limited`, `logout` (M02); `category_created`,
`category_updated`, `category_deactivated`, `menu_item_created`,
`menu_item_updated`, `menu_item_availability_changed`,
`ingredient_changed` (M03); `order_created`, `order_creation_failed` (M04).

**`user_id` means "actor" for the M03/M04 events**, not "subject" — it is
the id of the admin who performed a menu-management action, or the
customer who placed (or failed to place) an order. Auth events keep the
M02 meaning (the account the event is about). All readings coexist without
a schema change because the column has always meant "the account most
relevant to this row."

**Order events, specifically (M04):** `order_created` fires once per
successful checkout with `{"order_id": ..., "total": "..."}` in
`metadata_json` — enough to look up the order, nothing about its contents.
`order_creation_failed` fires whenever `checkout()` raises `CheckoutError`
(empty cart, an item that went unavailable mid-checkout, a quantity out of
bounds, or a database failure during the write) with no metadata beyond
the acting user — deliberately generic, since the failure reasons
themselves are already shown to the customer as flashed form errors and
don't need duplicating into the audit trail. `tests/test_orders.py` test
24 asserts the audit row's `user_id` and that the order id appears in
`metadata_json`; test 25 asserts a checkout failure (item deactivated
after being added to the cart) produces exactly one `order_creation_failed`
row.

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
operators. Automatic purging is future work — see §17.

---

## 12. Debug and environment separation

- `DEBUG` is `True` only in `DevelopmentConfig`.
- `ProductionConfig` hard-codes `DEBUG = False` / `TESTING = False` and its
  `validate()` raises if either is somehow true.
- `run.py` passes `debug=app.config["DEBUG"]` — it never hard-codes `True`.
- Production is served by a WSGI server (`gunicorn "app:create_app('production')"`),
  never by `run.py`.

**Why:** the Werkzeug debugger exposes an interactive Python console on any
traceback. Debug mode in production is remote code execution.

---

## 13. Error handling

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

## 14. Security logging

`app/utils/logging.py` provides two channels:

- `app.logger` → console + `logs/app.log` (rotating, 2 MiB × 5).
- `quickjunction.security` → console + `logs/security.log` (rotating,
  always at least `INFO`, non-propagating) — now actively written to by
  every audit event (§11).

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

## 15. Health endpoint

`GET /health` returns exactly `{"status": "ok"}`. Unchanged from Milestone
01: no version, environment name, hostname, path, configuration value, or
database status.

---

## 16. Protection against accidental secret commits

Unchanged from Milestone 01 — `.gitignore` coverage, no real `.env` in the
repository. See the project `README.md` for the current rule set.

---

## 17. Not yet implemented — required before production

Ranked by how much damage the absence causes. Items completed this
milestone (authentication, authorization, CSRF, session fixation
protection, an initial rate limiter, and — as of M04 — order audit
logging) are removed from this list.

| # | Item | Needed by |
| --- | --- | --- |
| 1 | Payment gateway / online card processing | first real transaction |
| 2 | Multi-worker-safe rate limiting (Flask-Limiter + Redis) | first multi-worker/multi-instance deployment |
| 3 | Security headers: HSTS, `X-Content-Type-Options`, `X-Frame-Options`, CSP (via Flask-Talisman or the reverse proxy) | first HTML page shipped to real users |
| 4 | Strip the `Server` header — the dev server advertises `Werkzeug/... Python/...` | deployment |
| 5 | TLS termination + HTTP→HTTPS redirect | deployment |
| 6 | A dedicated MySQL user with least privilege — no `GRANT ALL` in production | deployment |
| 7 | Password reset flow (with its own rate limiting and token expiry) | first locked-out real user |
| 8 | Email verification at registration | before email is trusted for anything (e.g. password reset) |
| 9 | Login-time rehash (`needs_rehash()` is written but not called) | first Argon2 parameter change |
| 10 | A maintained breached-password check (e.g. HaveIBeenPwned k-anonymity) replacing the hand-typed deny-list | before it matters for a real user base |
| 11 | Audit log retention/purge policy, enforced in code | first real data / compliance review |
| 12 | Encrypted, tested database backups | first real data |
| 13 | Dependency scanning (`pip-audit`) in CI | first CI run |
| 14 | Content negotiation on error responses (HTML vs JSON) | first real HTML page beyond auth forms |
| 15 | **MySQL verification of the M02+M03+M04 migrations** (`CHECK` constraints, `Numeric` price/subtotal/total types, foreign keys, indexes) — no local MySQL server was reachable in this environment; see `docs/DATABASE.md` | before any real (non-SQLite) deployment |
| 16 | Ingredient administration beyond the menu-item form's comma-separated field (rename/merge/delete an ingredient independently of any one menu item) | first time a typo'd ingredient name needs correcting across many items |
| 17 | Per-field price-change audit history (currently a price change is folded into the generic `menu_item_updated` event, not its own event) | if price-change history becomes a compliance or dispute-resolution need |
| 18 | Order cancellation (customer- or staff-initiated) | first customer who needs to cancel |
| 19 | Staff order-status management (`OrderStatus` already supports the full lifecycle — see `docs/ARCHITECTURE.md`) | Milestone 05 |
| 20 | Cart persistence across login (an anonymous cart is discarded on login/logout since `login_user()` clears the session — see `docs/SECURITY.md` §3) | first user who builds a cart before signing in |
| 21 | Multi-worker-safe checkout idempotency (a double-submitted checkout under concurrent requests relies on the cart being cleared client-side and server-side after the first success, not on a dedicated idempotency key) | first multi-worker/multi-instance deployment |

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
