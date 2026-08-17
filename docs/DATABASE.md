# Database — Milestone 05

Covers every model group that exists so far: the Milestone 02 auth/audit
core, the Milestone 03 menu data layer, and this milestone's cart/order
tables. No cart table exists -- the cart is session-only (see
`app/utils/cart.py` and `docs/ARCHITECTURE.md`); `orders`/`order_items` are
the only new tables.

---

## Entity overview

```
User ──1:1── CustomerPreference
  │
  ├──1:N── AuditLog (nullable FK; actor or subject, see below)
  │
  └──1:N── Order ──1:N── OrderItem ──N:1── MenuItem
                            (RESTRICT: a menu item referenced by an
                             order line can never be deleted)

Category ──1:N── MenuItem ──M:N── Ingredient
                     │              (through MenuItemIngredient)
                     └── (no FK to User; ownership is not modeled --
                          menu items belong to the restaurant, not a person)
```

| Table | Purpose | Added |
| --- | --- | --- |
| `users` | Accounts, roles | M02 |
| `audit_logs` | Security audit trail | M02 (extended M03, M04, M05) |
| `categories` | Menu categories | M03 |
| `menu_items` | Menu items | M03 |
| `ingredients` | Ingredient names | M03 |
| `menu_item_ingredients` | MenuItem ↔ Ingredient join | M03 |
| `customer_preferences` | One row per customer's stated preferences | M03 |
| `orders` | One row per placed order | M04 |
| `order_items` | Order line items, with price snapshots | M04 |

---

## Categories

`app/models/category.py`

| Column | Type | Constraint |
| --- | --- | --- |
| `id` | `INTEGER` | PK |
| `name` | `VARCHAR(80)` | `UNIQUE`, `NOT NULL` |
| `description` | `VARCHAR(500)` | nullable |
| `is_active` | `BOOLEAN` | `NOT NULL`, default `true` |
| `created_at` / `updated_at` | `DATETIME` | `NOT NULL`, server-managed |

**Uniqueness** is a real database `UNIQUE` constraint on `name`, not just an
application-level pre-check (`app/services/menu.py` pre-checks for a
friendly error, then the constraint is the backstop against the
check-then-insert race — same pattern as `users.username`/`email` in M02).

**Deactivation, not deletion**: there is no delete route. `is_active=False`
is how a category is retired; `menu_items.category_id` has
`ON DELETE RESTRICT`, so the database refuses to delete a category that
still has items regardless (verified in `tests/test_menu.py::test_6`, see
below).

---

## Menu items

`app/models/menu_item.py`

| Column | Type | Constraint |
| --- | --- | --- |
| `id` | `INTEGER` | PK |
| `category_id` | `INTEGER` | `FK categories.id ON DELETE RESTRICT`, `NOT NULL` |
| `name` | `VARCHAR(120)` | `NOT NULL` |
| `description` | `VARCHAR(2000)` | nullable |
| `price` | `NUMERIC(10, 2)` | `NOT NULL`, `CHECK (price > 0)` |
| `cuisine` | `VARCHAR(20)` | `NOT NULL`, `CHECK` (allow-list, see below) |
| `spice_level` | `VARCHAR(16)` | `NOT NULL`, `CHECK` (allow-list) |
| `dietary_type` | `VARCHAR(20)` | `NOT NULL`, `CHECK` (allow-list) |
| `is_available` | `BOOLEAN` | `NOT NULL`, default `true` |
| `created_at` / `updated_at` | `DATETIME` | `NOT NULL`, server-managed |

**Indexes**: one composite index, `ix_menu_items_category_available` on
`(category_id, is_available)`. No standalone index on `category_id` alone
-- the composite index already serves a category-only lookup via its
leftmost column, so a second index would be redundant ("do not
over-index," per the brief). This one index backs the public menu's only
real query shape: items in a category that are currently available.

**Money is never `float`.** `price` is `Numeric(10, 2)` end to end, mapped
to Python's `Decimal`. `tests/test_menu.py::test_9` inserts `10.10`,
reloads it from a fresh query, and asserts the string form is exactly
`"10.10"` -- the classic `float` failure mode (`10.099999999999998`) is
structurally impossible here, not just avoided by convention.

**Price is validated server-side, three ways**, per the brief's "never
trust a client-supplied price": (1) `app/services/menu.py`'s
`_validate_price` parses and range-checks every price before it reaches
the database; (2) the `CHECK (price > 0)` constraint is the database-level
backstop even against a direct/careless insert; (3) the public routes
(`app/routes/menu.py`) never read a price from the request at all -- there
is no code path for a client to submit one. `test_17` proves a spoofed
`?price=` query parameter on the public detail route has no effect.

---

## Allow-listed enum columns (the `CHECK`-constraint pattern)

`cuisine`, `spice_level`, `dietary_type` (on both `menu_items` and
`customer_preferences`), plus `users.role` and `audit_logs.event_type`
(M02), all go through one helper: `enum_column()` in `app/models/enums.py`.

```python
sa.Enum(
    enum_cls,
    name=name,                 # explicit, unique per column -- see below
    native_enum=False,         # VARCHAR, not a DB-native ENUM type
    create_constraint=True,    # emit an actual CHECK constraint
    values_callable=lambda cls: [m.value for m in cls],
)
```

Three non-obvious things had to be true together, found by inspecting the
actual emitted DDL rather than assuming the type behaved as documented:

1. **`create_constraint=True` is not the default** in SQLAlchemy 2.0.
   `native_enum=False` alone silently produces a bare `VARCHAR` with *no*
   database-level enforcement -- server-side Python validation still ran,
   but nothing stopped a raw `INSERT` from writing an arbitrary string.
   This was true of `users.role` and `audit_logs.event_type` from the
   moment M02 created them; both were retrofitted with a real `CHECK` in
   this milestone's migration (see below) rather than left silently
   unconstrained.
2. **`values_callable` is required** for the stored string to be the
   enum's `.value` (e.g. `"admin"`). Without it, SQLAlchemy stores the
   member's `.name` (`"ADMIN"`) -- which then disagrees with any
   `server_default` written as the value.
3. **The constraint name must be explicit and column-specific.** Left to
   its default, SQLAlchemy names the constraint after the *enum class*
   (e.g. `"cuisine"`) -- and `Cuisine` backs two different columns
   (`menu_items.cuisine`, `customer_preferences.cuisine_preference`), so
   both would generate the identical constraint name. SQLite tolerates
   that (names are scoped per-table there); MySQL requires `CHECK`
   constraint names to be unique **per schema**, so the second
   `CREATE TABLE` would fail outright. Every call site passes
   `ck_<table>_<column>` explicitly.

Every constraint is verified two ways in `tests/test_menu.py`: the service
layer's own allow-list check (`test_7`), and a raw SQL `INSERT` that
bypasses both the service *and* SQLAlchemy's Python-side validation
(`test_7b`) -- proving the database itself rejects the value, not just the
application in front of it.

---

## Ingredients and the many-to-many join

`app/models/ingredient.py`, `app/models/menu_item_ingredient.py`

`ingredients`: `id`, `name` (`VARCHAR(80)`, `UNIQUE`, `NOT NULL`),
`created_at`. No `updated_at` -- unlike `Category`/`MenuItem`, an
ingredient name is a lookup value, not a mutable business record.

`menu_item_ingredients`: a pure join table, `(menu_item_id, ingredient_id)`
as a **composite primary key** -- that alone both enforces "a menu item
lists an ingredient at most once" and is the table's only index, so no
separate `UNIQUE` constraint was needed. Both foreign keys are
`ON DELETE CASCADE`: deleting a menu item or an ingredient removes the
association automatically rather than leaving an orphaned row.

`app/services/menu.py`'s `sync_menu_item_ingredients()` is the only writer:
ingredient names are normalized (trimmed, lowercased) and find-or-created,
so the same ingredient typed on two different menu items resolves to one
`Ingredient` row, not two (`tests/test_menu.py::test_4`).

---

## Customer preferences

`app/models/customer_preference.py`

One row per customer: `user_id` (`FK users.id ON DELETE CASCADE`,
`UNIQUE`), plus three **nullable** allow-listed dimensions --
`dietary_preference`, `cuisine_preference`, `spice_preference` -- reusing
the exact same `DietaryType`/`Cuisine`/`SpiceLevel` enums as `MenuItem`.
Nullable here (unlike on `MenuItem`, where every field is required)
because a customer may not have stated a preference yet.

**Data minimization**: no name, phone number, or address is collected --
`users` already has everything needed to identify the account, and this
table adds nothing beyond taste. No CRUD routes exist for this table in
Milestone 03 (the brief asked to "keep it minimal"); the model exists so
the schema and the recommendation engine's future join are already
correct, without speculative UI ahead of the milestone that needs it.

**Recommendation-related attributes**: this table's whole purpose is to be
one half of a future join. `dietary_preference`/`cuisine_preference`/
`spice_preference` on `CustomerPreference` compare directly against
`dietary_type`/`cuisine`/`spice_level` on `MenuItem` -- same enum, same
stored strings -- which is what will let a TF-IDF/cosine-similarity or
rule-based recommender (a later milestone) match a customer's stated taste
against the menu without a translation layer in between.

---

## Orders

`app/models/order.py`

### `orders`

| Column | Type | Constraint |
| --- | --- | --- |
| `id` | `INTEGER` | PK |
| `user_id` | `INTEGER` | `FK users.id ON DELETE RESTRICT`, `NOT NULL` |
| `status` | `VARCHAR(16)` | `NOT NULL`, default `'pending'`, `CHECK` (allow-list) |
| `subtotal` | `NUMERIC(10, 2)` | `NOT NULL`, `CHECK (subtotal >= 0)` |
| `total` | `NUMERIC(10, 2)` | `NOT NULL`, `CHECK (total >= 0)` |
| `created_at` / `updated_at` | `DATETIME` | `NOT NULL`, server-managed |

**Index**: `ix_orders_user_created` on `(user_id, created_at)` -- backs the
one query `GET /orders` runs: a user's own orders, newest first.

**`user_id` is `ON DELETE RESTRICT`**, not `CASCADE` or `SET NULL`: an
order is a financial record and must never be silently orphaned or deleted
as a side effect of something happening to the account. There is no
user-delete route anywhere in this codebase, so this is a documented
invariant rather than one exercised by a test today.

**`status`** is a `CHECK`-constrained allow-list via the same
`enum_column()` helper as every other enumerated column (see "Allow-listed
enum columns" above), holding `OrderStatus`'s full six-value restaurant
lifecycle (`pending`, `confirmed`, `preparing`, `ready`, `completed`,
`cancelled`). Checkout writes only `pending`; M05's staff endpoint moves it
along the workflow. That forward planning paid off exactly as intended --
M05 added staff status management with **no change to `orders` at all**,
only a widening of the unrelated `audit_logs` event allow-list.

Which transitions are legal is enforced in `app/services/orders.py`
(`ALLOWED_STATUS_TRANSITIONS`), not by the database. The `CHECK` constraint
guarantees the column only ever holds a *known* status; it deliberately
does not encode the workflow, since a `CHECK` cannot compare against the
row's previous value. The application is the only writer of this column.

**`subtotal`/`total`**: both `Numeric(10, 2)` -> `Decimal`, same
never-`float` rule as `menu_items.price`. `total` equals `subtotal` in this
milestone (no tax, discount, or delivery fee exists yet) but is its own
column, not a derived value, so a future fee/discount milestone has
somewhere to diverge without a schema change.

### `order_items`

| Column | Type | Constraint |
| --- | --- | --- |
| `id` | `INTEGER` | PK |
| `order_id` | `INTEGER` | `FK orders.id ON DELETE CASCADE`, `NOT NULL`, indexed |
| `menu_item_id` | `INTEGER` | `FK menu_items.id ON DELETE RESTRICT`, `NOT NULL`, indexed |
| `item_name_snapshot` | `VARCHAR(120)` | `NOT NULL` |
| `unit_price_snapshot` | `NUMERIC(10, 2)` | `NOT NULL`, `CHECK (>= 0)` |
| `quantity` | `INTEGER` | `NOT NULL`, `CHECK (quantity > 0)` |
| `line_total` | `NUMERIC(10, 2)` | `NOT NULL`, `CHECK (>= 0)` |

**`order_id`** is `ON DELETE CASCADE`: deleting an order (no route does
this today) removes its line items with it -- the same compositional
relationship as `Category`/`MenuItem` and their child rows elsewhere in
this schema. **`menu_item_id`** is `ON DELETE RESTRICT`: menu items are
never deleted anywhere in this codebase (only deactivated via
`is_available`), and a historical order line must never be allowed to lose
its reference even if that changes later.

**Price snapshots, not live references**: `item_name_snapshot` and
`unit_price_snapshot` are copied from `MenuItem` at checkout time and never
updated again. A later rename or price change on the live menu item must
never rewrite what a customer was actually charged --
`tests/test_orders.py::test_18_price_snapshot_stored` changes the menu
item's name and price after checkout and confirms the stored order line is
unaffected.

**The trust boundary**: every value on `OrderItem` -- `unit_price_snapshot`,
`quantity`, `line_total` -- is computed server-side in
`app/services/orders.py::checkout` from the current `MenuItem` row, never
accepted from client-submitted form data. There is no code path anywhere
in `app/routes/orders.py` that reads a price, a subtotal, or a total from
the request. See `docs/SECURITY.md` for the full writeup, including what a
client-submitted price/subtotal/total field is guaranteed to have zero
effect on.

**No cart table**: the cart is session-only, storing nothing but
`{menu_item_id: quantity}` in Flask's signed session cookie (see
`app/utils/cart.py` and `docs/ARCHITECTURE.md`'s cart/checkout section).
Every price is re-resolved against `MenuItem` on every read, so no table is
needed to hold a value that must never be trusted anyway.

---

## Migration

`migrations/versions/8d5ba0171efe_...py` (`down_revision = abf997064564`,
the M02 migration).

Two parts:

1. **Auto-generated**: the five new tables and the composite/secondary
   indexes.
2. **Hand-written**: `ADD CONSTRAINT` for `ck_users_role` and
   `ck_audit_logs_event_type` on the two *pre-existing* M02 tables.
   Alembic's autogenerate did not detect either as a diff -- confirmed by
   generating twice and inspecting the result, not assumed. CHECK
   constraint diffing on existing columns is a known Alembic weak spot;
   these two statements were written by hand and are called out in a
   comment at the top of `upgrade()`/bottom of `downgrade()` so a future
   `flask db migrate` doesn't silently drop them by regenerating over top.

Verified this milestone (see the final report for exact commands): a full
`upgrade` from empty, `downgrade` back to empty, and re-`upgrade`, against
a scratch SQLite database -- no MySQL server was reachable in this
environment. **MySQL verification is pending**; see `docs/SECURITY.md` and
the final report for what specifically still needs checking against a
real MySQL instance (native `CHECK` constraint support requires MySQL
≥ 8.0.16; the schema targets exactly that).

**M04**: `migrations/versions/d8f3bfd0e2a9_add_cart_order_tables.py`
(`down_revision = 8d5ba0171efe`) adds `orders`/`order_items` and widens
`ck_audit_logs_event_type` to include `order_created`/`order_creation_failed`
(`AuditEvent` gained those two members this milestone). Autogenerate also
reported drop/recreate on three *unrelated* pre-existing constraints
(`ck_users_role`, `ck_menu_items_*`, `ck_customer_preferences_*`) -- the
same textual-diff false positive as M03's retrofit, confirmed by nothing
about those columns having changed; those three spurious pairs were
removed by hand, and only the genuine `ck_audit_logs_event_type` widening
was kept. This distinction was caught the hard way: a live checkout in
manual browser testing raised `IntegrityError` against a database migrated
with all four constraint changes stripped, because the audit-log widening
is real and the other three are not -- autogenerate's output alone cannot
tell the two apart, only knowing which enums actually changed can. Verified
with the same empty → upgrade → downgrade → re-upgrade round-trip against a
scratch SQLite database; MySQL verification is still pending (see below).

**M05**: `migrations/versions/38297b707b89_widen_audit_event_allow_list_for_order_.py`
(`down_revision = d8f3bfd0e2a9`) widens `ck_audit_logs_event_type` once more,
for `order_status_changed` / `order_status_change_rejected`. **No table
changed** — staff order management needed no schema change beyond the audit
vocabulary, because `orders.status` already carried the full lifecycle.

Written with `flask db revision` (no `--autogenerate`) deliberately: as
recorded above, autogenerate reports this constraint as "changed" on every
run whether or not it has, so its output cannot distinguish a real widening
from a false positive. Its `downgrade()` deletes any `order_status_*` audit
rows before narrowing the constraint — those rows cannot be represented
under the older allow-list, and failing the downgrade silently would be
worse than dropping audit history the schema no longer models.

Verified: empty → all four revisions → downgrade one → re-upgrade →
downgrade to base → re-upgrade, all clean against scratch SQLite, plus the
automated checks below.

**Migrations are now covered by the test suite** (`tests/test_migrations.py`,
added in M05 to close the gap that produced the M04 bug). Four tests run the
real Alembic chain against a temporary on-disk SQLite database and assert
that (1) it runs from empty, (2) the resulting schema — columns, foreign
keys, indexes and CHECK constraints — matches what `db.create_all()` builds
from the models, (3) every `AuditEvent` member satisfies the migrated
`ck_audit_logs_event_type`, and (4) downgrade-to-base then re-upgrade
reproduces the identical schema. Test (3) was confirmed to actually fail
when an un-migrated enum member is introduced, rather than passing
vacuously.

---

## Security considerations specific to this data layer

- **Authorization, not row ownership**: menu data has no owning user (it
  belongs to the restaurant), so there is no row-level "is this yours"
  check to get wrong here -- authorization is purely role-based
  (`app/utils/authorization.py`'s `require_role`), and is documented in
  full in `docs/SECURITY.md`.
- **SQL injection**: every query in `app/services/menu.py` goes through
  the SQLAlchemy ORM with bound parameters; nothing concatenates request
  data into SQL. `tests/test_menu.py::test_19` submits
  `"Drinks'; DROP TABLE categories; --"` as a category name through the
  real HTTP route and confirms it is stored as an inert string and the
  table still exists afterward.
- **XSS**: Jinja2 autoescaping is on everywhere (no template uses `|safe`).
  `test_20` submits `<script>alert(1)</script>` as a menu item name and
  confirms the public menu page renders it as literal text
  (`&lt;script&gt;`), never as markup -- verified against the running
  dev server in a real browser as well as in the test suite.
- **CHECK-constraint enforcement is proven at the database, not assumed
  from SQLAlchemy's type declaration** -- see "Allow-listed enum columns"
  above. This is the one modeling mistake this milestone caught and fixed
  before it reached a migration that anyone would run against real data.
- **Orders *do* have row ownership, unlike menu data** (M04):
  `orders.user_id` is the one column in this schema an authorization
  decision reads at the row level, and every query that returns an `Order`
  filters by it in the query itself (`app/services/orders.py`'s
  `list_orders_for_user`/`get_order_for_user`) rather than fetching by id
  and checking ownership after the fact -- the difference between a
  mismatched id returning `None`/404 and it ever being fetchable at all.
  `tests/test_orders.py::test_21_idor_attempt_rejected` logs in as a second
  account and confirms a direct `GET /orders/<id>` for someone else's order
  is a plain 404, not a 403 (which would itself disclose that the id
  exists).
- **Every monetary value on `Order`/`OrderItem` is server-computed** (M04):
  see the "Orders" section above and `docs/SECURITY.md` for the full
  writeup of what a client can and cannot influence at checkout.
