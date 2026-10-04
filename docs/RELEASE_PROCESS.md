# Release Process

How changes reach `main` and how releases are cut. Upgrade and rollback
procedures for installations are a later milestone (roadmap Phase 4).

## Branching

- `main` is always releasable.
- All work happens on short-lived branches named `<type>/<topic>`, e.g.
  `feat/create-admin`, `fix/reservation-cap`, `chore/m0-production-foundation`.
- Changes reach `main` only through a pull request with CI green.
- No history rewriting on `main` or on release tags.

## Commit messages

Conventional style: `type(scope): summary` — `feat`, `fix`, `chore`, `docs`,
`test`, `refactor`, `security`.

## Versioning

[Semantic Versioning](https://semver.org/):

- **MAJOR** — breaking change for operators or data.
- **MINOR** — new features or any database migration.
- **PATCH** — fixes with no migration.
- Before 1.0.0 a minor release may break things. **1.0.0 is the first release
  approved for a pilot restaurant** (all of `docs/PRODUCTION_READINESS.md` READY).

## Cutting a release

1. Every change for the release is merged to `main`; CI is green.
2. Move `CHANGELOG.md` entries from **[Unreleased]** to a new version heading
   with the date. Fill in **Migration** and **Operator action required**
   whenever they apply.
3. Merge that change through a pull request.
4. Create an annotated tag on the merge commit and push it:

   ```
   git tag -a vX.Y.Z -m "Quick Junction vX.Y.Z — <one-line summary>"
   git push origin vX.Y.Z
   ```

5. Create a GitHub release from the tag, with the changelog section as its
   description.

Tags are never moved or deleted once pushed. A bad release is fixed by a new
patch release.

## `main` branch protection (intended rules)

These settings could **not** be applied or verified from the development
machine (no GitHub CLI or API access). They must be enabled manually by the
repository owner and then confirmed in the repository settings.

GitHub → repository **Settings → Rules → Rulesets → New branch ruleset**
(or **Settings → Branches → Add branch protection rule**), target `main`:

| Setting | Value |
| --- | --- |
| Enforcement status | Active |
| Target branches | `main` (Default branch) |
| Restrict deletions | On |
| Block force pushes | On |
| Require a pull request before merging | On — required approvals: 1 once a second maintainer exists (0 while there is a single maintainer); dismiss stale approvals on new commits |
| Require status checks to pass | On — "Require branches to be up to date"; required checks: **Tests (SQLite)**, **Tests (MySQL 8.0)**, **Dependency audit (pip-audit)** |
| Require conversation resolution | On |
| Bypass list | Empty (administrators follow the same rules) |

The required check names appear in the picker only after the CI workflow has
run at least once on GitHub.

**Status: NOT VERIFIED.** Until the owner confirms these settings,
`docs/STATUS.md` and `docs/PRODUCTION_READINESS.md` (item R7) must keep saying so.
