# Database — Milestone 03

Covers the two model groups that exist so far: the Milestone 02 auth/audit
core, and this milestone's menu data layer. No business table beyond menu
data exists yet (no cart, order, or payment table).

---

## Entity overview

```
User ──1:1── CustomerPreference
  │
  └──1:N── AuditLog (nullable FK; actor or subject, see below)

Category ──1:N── MenuItem ──M:N── Ingredient
                     │              (through MenuItemIngredient)
                     └── (no FK to User; ownership is not modeled --
                          menu items belong to the restaurant, not a person)
```

| Table | Purpose | Added |
| --- | --- | --- |
| `users` | Accounts, roles | M02 |
| `audit_logs` | Security audit trail | M02 (extended M03) |
| `categories` | Menu categories | M03 |
| `menu_items` | Menu items | M03 |
| `ingredients` | Ingredient names | M03 |
| `menu_item_ingredients` | MenuItem ↔ Ingredient join | M03 |
| `customer_preferences` | One row per customer's stated preferences | M03 |

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
