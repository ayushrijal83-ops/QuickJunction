# Phase 3 — Product improvements and role-based access

Date: 2026-09-27. Migration head after this phase: `7c4e1a9b52d3`.

The production AI is untouched: same base model, same V4 LoRA adapter, same
`DEFAULT_DATASET_VERSION = "v4"`, same preference safeguard, same pinned AI
dependencies. No training was run.

## 1. Recommendation scoring

**What was wrong.** The page printed the TF-IDF cosine as "% match", and
labelled any positive cosine "Fair match". The cosine is computed over a whole
item document (name, description, category, ingredients, attribute tokens), so
it depends on description length and on how rare a token is on the current
menu. On the demo menu:

* a dish matching **all three** stated preferences scored 26%;
* a dish matching **one** preference scored 62%;
* a dish matching **none** of three was shown as "Fair match 2%".

**What it is now** (`app/services/recommendations.py`):

* **Preference match** (shown) — the mean over the preferences the customer
  actually set of:
  * cuisine: 1 if the same cuisine, else 0;
  * spice: 1 if the same level, 0.5 if one step apart, else 0;
  * diet: 1 if the exact dietary type, 0.5 if compatible but different.
    (Incompatible dishes are still removed by the dietary hard filter before
    any scoring.)
* **TF-IDF relevance** (internal) — the original cosine, kept as the
  tie-breaker within a match level and as the channel for order history.
  Hardened so it is always finite and in [0, 1], and returns zeros instead of
  raising on an empty vocabulary.
* **Ranking** — match score, then relevance, then name, then id.

**Cuisine similarity.** Cuisine is a fixed vocabulary, so exact equality is the
similarity. A TF-IDF "cuisine profile" approach was prototyped and rejected:
it rated Chicken Fajitas 31% similar to Indian and Steamed Rice 19% similar to
Italian through shared words ("chicken", "onion", "rice") alone.

**Labels and thresholds** — applied to the same rounded whole-number
percentage the page shows, so badge and number always agree:

| Percent | Label sent to the model | Shown on the page |
| --- | --- | --- |
| 80–100 | Strong match | Strong match |
| 60–79 | Good match | Good match |
| 40–59 | Fair match | Fair match |
| 0–39 | Suggested | **Low match** |
| no preferences set | Suggested | Suggested |

With three preferences set the score can be 100 (3/3), 83 (2 + a near spice),
67 (2 of 3), 50, 33 (1 of 3), 17 or 0 — so "Fair" means at least half of what
was asked for, and one of three is a low match. The model keeps receiving
exactly the four label strings it was trained on.

The percentage describes fit to stated preferences; it is not a probability
that the customer will like the dish, and the page says so.

## 2. Sign-in portals and roles

* `/login` — customer sign-in (also the general entry point: any account can
  sign in there and lands on its own dashboard).
* `/login/staff`, `/login/admin` — refuse accounts of any other role.
* One login view serves all three: one password check, one rate limiter, one
  session implementation. The portal never grants a role.
* After sign-in, every role lands on `/account/`, which is a role-specific
  dashboard (customer / staff / admin). Safe `next` redirects are preserved and
  can no longer point at any login page; the destination still enforces its
  own role.
* **Preferences and food suggestions are customer-only**:
  `/preferences`, `/recommendations` and `/recommendations/explain` use
  `customer_required`, and staff/admin navigation, dashboards and the home
  page no longer link to them.

## 3. Staff approval

* New column `users.staff_approved` (default false), migration `7c4e1a9b52d3`.
* `/register/staff` creates a STAFF account that starts **pending** and is not
  signed in. There is no request path that can create an admin, and form fields
  such as `role`, `is_admin` or `staff_approved` are ignored.
* A pending staff account cannot sign in (it is told approval is required —
  only after the correct password). Enforcement is in `get_current_user`, so
  a staff member whose approval is revoked loses every route on their next
  request, not just staff routes.
* Admins manage accounts at `/admin/staff`: pending and approved lists with
  registration date, **Approve** and **Revoke access** buttons (POST +
  CSRF), each recorded in the audit log (`staff_approved`,
  `staff_approval_revoked`, `staff_registered`).
* **Existing accounts on upgrade:** staff that exist when the migration runs
  are marked approved, because until now a staff account could only be created
  by an operator (seed script or direct SQL). Customers and admins are
  unaffected. `scripts/seed_demo.py` creates its `staff` account pre-approved.

## 4. MySQL application account

* The tracked code never defaulted to root; `DATABASE_URL` is required.
* New guard: if `DATABASE_URL`'s user is `root`, development logs a warning
  and `APP_ENV=production` refuses to start. The URL is never printed.
* `docs/MYSQL_SETUP_HANDOFF.md` §5.5 now shows a separate runtime account
  (`SELECT/INSERT/UPDATE/DELETE`) and migration account, and §5.6 explains how
  to move an existing installation off root without moving data.

## 5. Tests

```text
pytest -q
```

New files: `tests/test_match_scoring.py`, `tests/test_staff_approval.py`,
`tests/test_staff_migration.py`. `tests/conftest.py::make_user` now creates
staff accounts approved by default (they stand for operator-provisioned
accounts); pass `staff_approved=False` for a pending one.

## 6. Known limitations

* Staff self-registration is open to anyone; it grants nothing until an admin
  approves it, but there is no email verification or rate limit on sign-ups.
* Cuisine similarity is exact-match only; related cuisines get no partial
  credit, because the text-based approach was demonstrably unreliable on a
  menu this small.
* The upgrade approves every pre-existing staff account; if a database contains
  staff that should not be trusted, revoke them at `/admin/staff` after
  upgrading.
