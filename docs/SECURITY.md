# Security — Milestone 09 baseline

Scope: foundation (M01), auth/authorization/CSRF/audit core (M02), the menu
data layer and menu-management surface (M03), cart, checkout, order
placement, and order history (M04), staff order management and the status
workflow (M05), customer preferences and the deterministic recommendation
engine (M06), local Qwen inference for recommendation explanations (M07),
the Bootstrap UI and verified MySQL operation (M08), plus **server-side
session revocation (M09)**. No payment gateway exists yet. This document records what is in
place, **why**, and what must be added before Quick Junction handles real
payments.

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
applies; `logout_user()` calls `session.clear()`, so the browser that logged
out is immediately unauthenticated (test 20: a protected endpoint returns 401
right after logout, in the same test client / cookie jar).

**Session revocation — the M08 finding, fixed in M09.**

*The history, kept deliberately:* an early version of this section claimed
logout "invalidat[es] the old session immediately". That overstated what a
stateless signed cookie can do, and the **M08 audit disproved it**: a
`qj_session` value captured *before* logout still authenticated afterwards —
replaying it returned `200` from `/orders` and `/account/me` after the
original browser had logged out.

*Root cause:* the session was a signed cookie carrying only a user id, and
`get_current_user()` accepted any validly-signed cookie whose user existed and
was active. There was no server-side state to invalidate, so `session.clear()`
could only clear the browser's own copy — never a copy someone else held.

*The fix (M09):* `users.session_version`, an integer counter.

| Step | Behaviour |
| --- | --- |
| `login_user()` | stamps the session with the user's current `session_version` |
| every request | `get_current_user()` compares the session's version against the column; a mismatch clears the session and returns `None` |
| `logout_user()` | increments the column, so every session issued before that logout stops validating |

Nothing secret is stored or transmitted: the cookie gains an integer counter,
not a token, so there is no session secret that could leak. The counter is
never logged and never appears in a response body (asserted by
`tests/test_session_revocation.py` tests 7 and 7b).

*Scope — logout revokes **all** of that user's sessions.* A single counter
cannot distinguish one session from another, so logging out on one device logs
the account out everywhere. Per-session revocation would need per-session
server-side state (a session table, or a token in the cookie) — a materially
larger subsystem for a marginal usability gain. Erring toward revoking too
much is the safe direction, and the behaviour is pinned by
`test_9_logout_revokes_every_session_for_that_user` so it is an explicit
choice rather than an accident.

*Fail-closed on upgrade:* sessions issued before the migration carry no
version at all, so they fail the comparison and are refused. Every
pre-existing login is invalidated when M09 is deployed — intended.

*Verified* (M09), each with a control proving the replay harness works:

- Live against MySQL 8.0.46 across separate processes: capture → control
  replay `200` → logout → replay `401` → fresh login `200` → old cookie still
  `401`.
- The standing security audit's "old session cookie unusable after logout"
  check moved from **FAIL** to **PASS** (36/36).
- 18 regression tests in `tests/test_session_revocation.py`; disabling the
  version comparison makes exactly the three replay tests fail, so they are
  not vacuous.

*Still bounded by, not replaced by, the cookie attributes:* `HttpOnly` (a page
script cannot read `qj_session` — re-confirmed in the browser, `document.cookie`
is empty), `Secure` in production, `SameSite=Lax`, and the 8-hour lifetime.
Revocation is now the primary control rather than the only hope.

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

## 10a. Staff order management and the status workflow (M05)

**Authorization is one decorator, applied to every route.** All three staff
endpoints (`GET /staff/orders`, `GET /staff/orders/<id>`,
`POST /staff/orders/<id>/status`) carry
`@require_role(Role.STAFF, Role.ADMIN)` — the same mechanism the admin menu
routes have used since M03, reading the role from the `User` row re-loaded
from the database on every request. An anonymous request gets 401, a
CUSTOMER gets 403, and neither reaches the view body. ADMIN reuses the
staff interface and service functions rather than getting a second
implementation to keep in sync.

Nothing in this feature consults a client-supplied role. There is no header,
hidden field, cookie value, or template flag that influences the decision —
`tests/test_staff_orders.py::test_12` sends `X-Role: staff`,
`X-User-Role: admin`, `Role: admin` plus `role`/`is_staff`/`user_role` form
fields as a CUSTOMER and confirms a 403 with the order unchanged, and the
same forgery was re-run against the live server during browser testing. The
"Order queue (staff)" link on `/account/` is hidden from customers for
tidiness only; hiding it is not the control.

**Two authorization models, deliberately different.** Customer order routes
enforce ownership *inside the query* (`get_order_for_user`, §10) because
ownership is the authorization. Staff routes deliberately do **not** filter
by user — `get_order_for_staff` fetches any order by primary key, since
seeing every order is the job — and the role check is what gates them.
Mixing these up in either direction is the obvious way to introduce a bug
here, so they are separate functions with separate names rather than one
function with a flag. Customer-side IDOR protection is unaffected by M05:
`test_13` re-confirms a customer still gets 404 for another customer's
order and 403 for the staff route to it.

**The status workflow is a server-side allow-list**
(`ALLOWED_STATUS_TRANSITIONS` in `app/services/orders.py`):

```
PENDING -> CONFIRMED|CANCELLED   CONFIRMED -> PREPARING|CANCELLED
PREPARING -> READY|CANCELLED     READY -> COMPLETED
COMPLETED -> (none)              CANCELLED -> (none)
```

Anything not in the table is refused: backwards moves
(`COMPLETED -> PREPARING`), skips (`PENDING -> COMPLETED`), same-status
no-ops, and any string outside the enum (`coerce_status` rejects it before
the transition check ever runs). Both terminal states map to an empty set,
so a finished or cancelled order can never move again.

**The UI is not the control.** The detail template renders only the
currently-legal buttons, but `update_order_status()` re-validates against
the order's real current status on every POST — a stale page, a
hand-crafted form, or a replayed request cannot produce a forbidden
transition. Verified live: a forged `COMPLETED -> PREPARING` POST carrying a
valid CSRF token was rejected and the order stayed `completed`.

**A status change never touches money.** It writes one column. The
`OrderItem` price snapshots from checkout are never recalculated from the
current menu — `test_15` repriced the live menu item to 999.00, walked the
order through all four transitions, and confirmed every snapshot, line
total, subtotal and total was unchanged.

**Customers can see their own status and change nothing.** `/orders` and
`/orders/<id>` show the current status (both re-verified in the browser
after a staff-driven change). The staff endpoint is the only status-change
route in the application and it is role-gated; no customer-facing status
route exists at all (`test_8` confirms 403 on the staff route and 404/405
on the plausible customer-side URLs).

**CSRF** protects the status endpoint like every other state-changing form
(`OrderStatusForm`, checked by `CSRFProtect` before the view runs).
Verified live with an authenticated ADMIN session: both a missing token and
a forged token returned 400 with the order unchanged.

**Errors disclose nothing.** `test_17` forces the queue to raise and
asserts the response is the generic JSON 500 with no exception name,
message, or traceback; a missing order id is a plain 404 (`test_18`).

---

## 10b. Preferences and the recommendation engine (M06)

> **The recommendation engine is deterministic and does not use the Local
> Qwen model yet.** No LLM, no external API, no network call, no GPU. That
> matters to this document because it removes an entire threat class for
> now: there is no prompt to inject, no model output to sanitise, and no
> customer data leaving the process.

**Identity comes from the session, never from the request.** Both routes
are `@login_required` and read the customer id from `get_current_user()`.
`app/routes/preferences.py` has no `user_id` parameter of any kind, and
`update_preferences(user_id, ...)` locates the row *by* that authenticated
id — there is no code path by which one customer reaches another's
preferences. `tests/test_recommendations.py` tests 4 and 18 submit a victim's
id as a form field, a query-string parameter **and** an `X-User-Id` header
while authenticated as someone else, and confirm the victim's row is
untouched and the change landed on the attacker's own row. Re-verified in
the browser: after a forged-id POST, the database held exactly one
preference row, belonging to the authenticated user.

**Dietary restriction is a hard filter, not a ranking hint.** It is applied
to the candidate set *before* any scoring
(`candidate_items` → `_DIETARY_COMPATIBILITY`), so neither TF-IDF
similarity nor order history can reintroduce a restricted item. Order
history is filtered by the same rule a second time before it is allowed to
influence the ranking, so a customer who has since gone vegetarian is not
nudged back toward meat by their own past orders. `vegetarian` excludes
`eggetarian` deliberately — the stricter reading is the safe one to be
wrong about. Tests 9, 21 and 21b cover this; **disabling the filter was
confirmed to fail five tests**, so the protection is genuinely asserted
rather than incidentally true.

**Only real, orderable items are ever returned.** Candidates come from one
query filtered to available items in active categories — the same filter
the public menu uses. The engine returns `MenuItem` rows, so it *cannot*
fabricate an item or an id (tests 13 and 15). Verified in the browser with
a deliberately adversarial fixture: "Sold Out Curry" matched the stated
preferences perfectly on every attribute but was correctly absent because
it is unavailable.

**The engine is advisory and never an authority on price.** It computes no
monetary value; `Recommendation` carries a similarity score and a menu item,
nothing else. The template reads `rec.menu_item.price` straight off the
database row. A customer has no input that can influence a price — there is
no price field anywhere in the preference form — and the engine never
creates or modifies an order. Test 14 asserts every rendered price equals
the stored `Decimal`; the browser walkthrough compared the three displayed
prices against the database directly. (The one `float()` in the engine is
on a cosine score, never on money.)

**Invalid preference values are rejected server-side.** Each value is
re-validated against its enum in `app/services/preferences.py` — the
`<select>` is one layer, not the only one, because a raw POST can skip it —
and the column's `CHECK` constraint is the database-level backstop. An
invalid value re-renders the form with errors and writes nothing (test 3,
3b; confirmed live with `carnivore`/`klingon`/`volcanic`). An empty string
is valid and means "no preference", stored as `NULL`.

**CSRF** protects preference changes like every other state-changing form
(test 17; confirmed live — a tokenless POST returned 400 and wrote
nothing).

**No sensitive customer data is logged.** The engine logs nothing at all,
and preference changes are deliberately **not** audit-logged: a taste
preference is not a security event, and adding one would have meant another
`ck_audit_logs_event_type` migration for no security benefit. Preferences
themselves are minimal by design — three enum values, no free text, no PII
(`docs/DATABASE.md`). Verified after the browser walkthrough:
`logs/security.log` and `logs/app.log` contained zero occurrences of
`password`, `argon2`, `csrf_token`, or `session`.

**Dependency note.** M06 promotes `scikit-learn` (+ `scipy`, `numpy`) from
the AI stack into `requirements.txt`, because `/recommendations` is a normal
customer-facing page that must work where the AI stack is absent. This is
~100 MB of new runtime dependency and therefore new supply-chain surface;
all three are pinned, and the rationale plus the swap-out path is recorded
in `requirements.txt` itself.

---

## 10c. Local LLM boundary (M07)

**The model has no authority.** `app/services/recommendations.py` decides
what a customer may be shown; `app/services/local_llm.py` only turns that
decision into prose. This is the control that makes every other item in this
section defence in depth rather than the primary protection: even a fully
compromised or hallucinating model cannot surface an item, change a price,
or alter an order, because it is never consulted about any of those things.

**It is given no price and no id.** `ExplanationRequest` is a frozen
dataclass carrying only item name, cuisine, dietary type, spice level and a
match label. There is no price field, so the model cannot restate a price
correctly *or* incorrectly. The page renders the price from the `MenuItem`
row (`tests/test_llm.py::test_8` feeds a hostile output claiming a different
price and name, then asserts the database row and the displayed price are
unchanged, and that no such item exists).

**Filtered items never reach it.** Unavailable and dietary-excluded items are
removed by the engine before the explanation route sees anything, so they
cannot appear in the prompt or the output (tests 10 and 11 assert the
`ExplanationRequest` names the surviving item, and that the excluded name
appears nowhere on the page).

**Prompt injection — re-verified in M07.1.** Two surfaces, both handled.
`tests/test_llm.py::test_12d` now parametrises **ten hostile shapes** against
the sanitiser: bare colons, newlines, CRLF, instruction-like text ("Ignore
previous instructions…"), quotes, HTML (`<script>`), template syntax
(`{{ 7*7 }}` / `{% raw %}`), markdown section markers (`###`), tab-separated
field forgery, and bracket/pipe markers. Each asserts that exactly one of
each real section header survives, that the item name occupies exactly one
line, that the structural field count is invariant, and that no structural
character reaches the prompt. Quotes are deliberately **not** stripped
(`test_12e`) — apostrophes are legitimate in dish names and cannot forge
`label: value` structure, which is what the sanitiser defends.

- *Menu text* (staff-supplied item names and descriptions) is the realistic
  one — an admin could name an item "Ignore previous instructions". Free text
  is flattened and truncated by `_sanitise()` before entering the prompt:
  newlines, tabs, markup characters **and colons** are stripped, and the
  value is capped at 120 characters. The colon is included deliberately —
  the prompt's structure is `label: value` lines, so a surviving colon is
  exactly what would let a hostile name forge a field. Stripping newlines
  alone was **not** sufficient; `tests/test_llm.py::test_12` caught that
  during development and now guards it (disabling the sanitiser makes it
  fail).
- *Customer preferences* are not a free-text surface at all: they are enum
  members re-validated server-side (§10b) and rendered from the enum, never
  echoed from request input.

Neither is treated as a complete defence. The real guarantee remains that
model output is displayed as escaped text and is never parsed, executed,
interpolated into SQL, or used to make a decision.

**Model paths are configuration, never request input.** `LLM_MODEL_PATH` and
`LLM_ADAPTER_PATH` come from `config.py` / the environment. No function in
`local_llm.py` takes a path argument and no route accepts one; test 4b sends
`?model_path=/etc/passwd&adapter_path=…` and asserts the configuration is
untouched and the signature has no such parameter. There is therefore no
arbitrary-file-read surface through the model loader.

**No network, no keys.** `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` are set
before `transformers` is imported, and both model and adapter load with
`local_files_only=True`. Test 13 greps the module for `openai`, `anthropic`,
`gemini`, `ollama`, `api_key`, `bearer`, `http://`, `https://`,
`requests.post` and `urllib.request` and fails if any appears; test 13b
applies the same check to the training script and additionally asserts
`report_to=[]` so no experiment-tracking service is contacted. There is no
API key in the codebase because there is no external service.

**Logging.** The module logs only that inference failed, via
`logger.warning(..., exc_info=True)` — no prompt, no customer preference, no
generated text. Prompts contain no PII by construction (no username, email,
id or order history — only menu attributes and enum values).

**Model quality is not a security control (M07.1).** M07's adapter asserted
that mismatched items matched; M07.1 retrained on a rebalanced dataset and
fixed it (5/8 → 8/8 on held-out scenarios, `docs/AI.md` §11). That is a
*quality* improvement, and the security posture does not depend on it: the
model still has no authority, is still given no price or id, and its output
is still displayed as escaped text that nothing parses. A future adapter
that regressed would produce misleading prose beside correct facts — not a
privilege escalation. Both adapters are kept on disk and the training script
refuses to overwrite an existing one, so a bad retrain cannot destroy the
artefact it replaces.

**Availability.** The 1.2 GB model is loaded lazily behind a lock, once per
process, and never in the test suite (`TestingConfig.LLM_ENABLED = False`).
CPU generation takes seconds, which is why the explanation lives on its own
route: `/recommendations` never invokes the model, so the primary
recommendation path cannot be slowed or broken by it. Failure returns `None`
and the page shows "AI explanation temporarily unavailable."

---

## 11. Audit logging

`app/models/audit_log.py` (`AuditLog`, `AuditEvent`) +
`app/services/audit.py` (`record_event` — the only writer).

**Recorded events:** `register_success`, `login_success`, `login_failure`,
`login_rate_limited`, `logout` (M02); `category_created`,
`category_updated`, `category_deactivated`, `menu_item_created`,
`menu_item_updated`, `menu_item_availability_changed`,
`ingredient_changed` (M03); `order_created`, `order_creation_failed` (M04);
`order_status_changed`, `order_status_change_rejected` (M05).

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

**Staff status events (M05):** `order_status_changed` records
`{"order_id", "from", "to"}` with `success=True`; `order_status_change_rejected`
records `{"order_id", "from", "attempted"}` with `success=False`.
**`user_id` is the acting staff/admin member, not the customer who owns the
order** — the audit trail answers "who moved this order", which is the
question that matters for a status change. Rejected attempts are logged
deliberately: a repeated attempt to force an order backwards is exactly the
pattern worth being able to see. The `attempted` value is client-supplied
and is truncated to 32 characters before storage.
`tests/test_staff_orders.py` tests 14 and 14b assert both row types, their
actor, their success flag, and that no credential material appears in
either; test 16 confirms a *denied* (403) attempt creates no
`order_status_changed` row at all. Confirmed independently by reading
`logs/security.log` and the `audit_logs` table after the browser
walkthrough: four changes and one rejection, with correct actor and
from/to values.

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

Ranked by how much damage the absence causes. Items completed in earlier
milestones (authentication, authorization, CSRF, session fixation
protection, an initial rate limiter, order audit logging in M04, and staff
order-status management in M05) are removed from this list.

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
| 15 | ~~MySQL verification~~ — **completed in M08** against MySQL 8.0.46: constraints enforced, decimal money, foreign keys, InnoDB | — |
| 16 | Ingredient administration beyond the menu-item form's comma-separated field (rename/merge/delete an ingredient independently of any one menu item) | first time a typo'd ingredient name needs correcting across many items |
| 17 | Per-field price-change audit history (currently a price change is folded into the generic `menu_item_updated` event, not its own event) | if price-change history becomes a compliance or dispute-resolution need |
| 18 | Order cancellation *handling* — M05 added the `CANCELLED` status transition, but nothing downstream of it: no refund, no restock, no customer notification, and no customer-initiated cancellation | first customer who needs to cancel a real order |
| 19 | Per-order staff assignment / "who is working this order" beyond the audit trail | if the kitchen needs accountability finer than "who last moved it" |
| 20 | Cart persistence across login (an anonymous cart is discarded on login/logout since `login_user()` clears the session — see `docs/SECURITY.md` §3) | first user who builds a cart before signing in |
| 21 | Multi-worker-safe checkout idempotency (a double-submitted checkout under concurrent requests relies on the cart being cleared client-side and server-side after the first success, not on a dedicated idempotency key) | first multi-worker/multi-instance deployment |

### AI-specific — status after M07

These were written before the model existed. Current state of each:

- Prompt injection → **addressed** (§10c): input sanitised, output never
  parsed or executed.
- No customer PII to the model → **held**: the prompt contains only menu
  attributes and enum values; no username, email, id, or order history.
- The model advises, never authorises → **held**: it is given no price and
  no id, and cannot write anything.
- Local inference only → **held**: offline flags, `local_files_only=True`,
  no HTTP client, no API key.
- `safetensors`, never `pickle` → **held**: the weights are
  `model.safetensors` and `safetensors==0.8.0` is pinned; the LoRA adapter
  is likewise safetensors.

Original rules, retained as the standing policy:

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
- **The M06 boundary, now that a recommender exists:** the LLM sits beside
  the deterministic engine, never inside it. Model output may never select
  the candidate set, never override the dietary hard filter, and never
  produce a price. `app/services/recommendations.py` remains the sole
  authority on what a customer may be shown; an LLM may only phrase or
  explain results it has already been handed.
