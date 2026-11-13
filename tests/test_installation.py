"""O1: local Organization / Branch / Installation identity -- models,
constraints, the read-only ``get_installation()`` and ``flask installation
show``.

Uses the standard ``app`` fixture (``create_all()``), so it runs on the
default SQLite and, when ``TEST_DATABASE_URL`` is set, on MySQL. That schema
holds no identity rows; ``identity`` below inserts them the way migration
4c7e2a9f1b38 does. The migration itself is covered by
tests/test_installation_migration.py.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.extensions import db as _db
from app.models.installation import Branch, Installation, Organization
from app.services.installation import InstallationIdentityError, get_installation
from tests.conftest import rejected_by_check_constraint

IDENTITY_TABLES = ("organizations", "branches", "installation")


@pytest.fixture
def identity(db):
    organization = Organization(name="Cafe Himalaya")
    branch = Branch(organization=organization, name="Cafe Himalaya")
    installation = Installation(id=1, branch=branch)
    _db.session.add_all([organization, branch, installation])
    _db.session.commit()
    return organization, branch, installation


def _rows(table):
    return _db.session.execute(sa.text(f"SELECT * FROM {table} ORDER BY id")).all()


def _snapshot():
    return {t: _rows(t) for t in IDENTITY_TABLES}


def _is_uuid(value):
    return str(uuid.UUID(value)) == value


# --- models and relationships ----------------------------------------------------------


def test_identity_records_link_together(identity):
    organization, branch, installation = identity
    assert installation.branch is branch and branch.organization is organization
    assert organization.branches == [branch]
    for public_id in (organization.public_id, branch.public_id, installation.public_id):
        assert _is_uuid(public_id)
    assert len({organization.public_id, branch.public_id, installation.public_id}) == 3
    assert organization.hq_id is None and branch.hq_id is None
    assert organization.created_at and branch.created_at and installation.created_at


def test_public_ids_are_generated_once_at_insert(db):
    organization = Organization(name="Org")
    assert organization.public_id is None  # nothing is generated before the row is written
    _db.session.add(organization)
    _db.session.commit()
    generated = organization.public_id
    _db.session.expire_all()
    assert _db.session.get(Organization, organization.id).public_id == generated


def test_public_id_cannot_be_changed_once_set(identity):
    organization, branch, installation = identity
    for record in (organization, branch, installation):
        with pytest.raises(ValueError, match="permanent"):
            record.public_id = str(uuid.uuid4())
    _db.session.rollback()


def test_organization_public_id_is_unique(identity):
    organization, _, _ = identity
    _db.session.add(Organization(name="Copy", public_id=organization.public_id))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_branch_public_id_is_unique(identity):
    organization, branch, _ = identity
    _db.session.add(Branch(organization_id=organization.id, name="Copy", public_id=branch.public_id))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_hq_ids_may_be_null_repeatedly_but_unique_once_set(identity):
    organization, branch, _ = identity
    # Several NULL hq_ids are fine (identity fixture already holds one of each).
    other_org = Organization(name="Other")
    _db.session.add(other_org)
    _db.session.add(Branch(organization=other_org, name="Other branch"))
    _db.session.commit()

    organization.hq_id = "hq-org-1"
    branch.hq_id = "hq-branch-1"
    _db.session.commit()

    other_org.hq_id = "hq-org-1"
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()

    other_branch = _db.session.execute(sa.select(Branch).filter_by(name="Other branch")).scalar_one()
    other_branch.hq_id = "hq-branch-1"
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_installation_is_a_singleton(identity):
    _, branch, _ = identity
    insert = sa.text("INSERT INTO installation (id, public_id, branch_id, created_at)"
                     " VALUES (1, :p, :b, CURRENT_TIMESTAMP)")
    with pytest.raises(IntegrityError):
        _db.session.execute(insert, {"p": str(uuid.uuid4()), "b": branch.id})  # a second id = 1: primary key
    _db.session.rollback()

    with rejected_by_check_constraint():
        _db.session.execute(sa.text("INSERT INTO installation (id, public_id, branch_id, created_at)"
                                    " VALUES (2, :p, :b, CURRENT_TIMESTAMP)"),
                            {"p": str(uuid.uuid4()), "b": branch.id})
    _db.session.rollback()
    assert _db.session.scalar(sa.text("SELECT COUNT(*) FROM installation")) == 1


def test_deleting_a_referenced_organization_is_restricted(identity):
    organization, _, _ = identity
    with pytest.raises(IntegrityError):
        _db.session.execute(sa.text("DELETE FROM organizations WHERE id = :i"), {"i": organization.id})
    _db.session.rollback()
    assert _db.session.scalar(sa.text("SELECT COUNT(*) FROM organizations")) == 1


def test_deleting_a_referenced_branch_is_restricted(identity):
    _, branch, _ = identity
    with pytest.raises(IntegrityError):
        _db.session.execute(sa.text("DELETE FROM branches WHERE id = :i"), {"i": branch.id})
    _db.session.rollback()
    assert _db.session.scalar(sa.text("SELECT COUNT(*) FROM branches")) == 1


def test_orm_delete_of_a_referenced_organization_is_also_restricted(identity):
    """The relationship must not null out or delete the branch behind the
    database's back; RESTRICT has to reach the database."""
    organization, _, _ = identity
    _db.session.delete(organization)
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()
    assert _db.session.scalar(sa.text("SELECT COUNT(*) FROM branches")) == 1


# --- get_installation() ------------------------------------------------------------------


def test_get_installation_returns_the_stored_identity(identity):
    organization, branch, installation = identity
    found = get_installation()
    assert found.organization_name == "Cafe Himalaya" and found.branch_name == "Cafe Himalaya"
    assert found.organization_public_id == organization.public_id
    assert found.branch_public_id == branch.public_id
    assert found.installation_public_id == installation.public_id
    assert found.organization_hq_id is None and found.branch_hq_id is None
    assert found.hq_linked is False


def test_repeated_reads_return_the_same_identity_and_write_nothing(identity):
    before = _snapshot()
    first = get_installation()
    for _ in range(5):
        _db.session.expire_all()
        assert get_installation() == first
    _db.session.rollback()
    assert _snapshot() == before


def test_identity_snapshot_is_immutable(identity):
    found = get_installation()
    with pytest.raises(AttributeError):
        found.installation_public_id = str(uuid.uuid4())


def test_missing_identity_fails_clearly_and_creates_nothing(db):
    with pytest.raises(InstallationIdentityError, match="No installation identity"):
        get_installation()
    assert all(_rows(t) == [] for t in IDENTITY_TABLES)


def test_invalid_uuid_fails_clearly_and_is_not_repaired(identity):
    _, _, installation = identity
    _db.session.execute(sa.text("UPDATE installation SET public_id = 'not-a-uuid' WHERE id = 1"))
    _db.session.commit()
    _db.session.expire_all()
    with pytest.raises(InstallationIdentityError, match="installation public id is not a valid UUID"):
        get_installation()
    assert _db.session.scalar(sa.text("SELECT public_id FROM installation")) == "not-a-uuid"


def test_non_canonical_uuid_is_rejected(identity):
    organization, _, _ = identity
    _db.session.execute(sa.text("UPDATE organizations SET public_id = :p WHERE id = :i"),
                        {"p": organization.public_id.upper(), "i": organization.id})
    _db.session.commit()
    _db.session.expire_all()
    with pytest.raises(InstallationIdentityError, match="organization public id"):
        get_installation()


def test_extra_organization_or_branch_is_reported(identity):
    extra = Organization(name="Stray")
    _db.session.add(extra)
    _db.session.commit()
    with pytest.raises(InstallationIdentityError, match="exactly one organization and one branch"):
        get_installation()


def test_missing_tables_fail_clearly(app):
    """A database that was never migrated (no identity tables at all)."""
    _db.session.execute(sa.text("DROP TABLE installation"))
    _db.session.commit()
    with pytest.raises(InstallationIdentityError, match="flask db upgrade"):
        get_installation()
    Installation.__table__.create(_db.engine)  # let the fixture's drop_all() finish normally


# --- flask installation show -----------------------------------------------------------


def test_cli_show_prints_the_identity_and_changes_nothing(app, identity):
    organization, branch, installation = identity
    before = _snapshot()
    _db.session.rollback()
    result = app.test_cli_runner().invoke(args=["installation", "show"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Organization:       Cafe Himalaya" in out
    assert f"  Public ID:        {organization.public_id}" in out
    assert f"  Public ID:        {branch.public_id}" in out
    assert f"Installation ID:    {installation.public_id}" in out
    assert out.count("HQ ID:            not linked") == 2
    assert "HQ linked:          no" in out
    _db.session.rollback()
    assert _snapshot() == before


def test_cli_show_reports_hq_links_when_present(app, identity):
    organization, branch, _ = identity
    organization.hq_id, branch.hq_id = "hq-org-7", "hq-branch-7"
    _db.session.commit()
    result = app.test_cli_runner().invoke(args=["installation", "show"])
    assert result.exit_code == 0, result.output
    assert "HQ ID:            hq-org-7" in result.output
    assert "HQ ID:            hq-branch-7" in result.output
    assert "HQ linked:          yes" in result.output


def test_cli_show_fails_clearly_without_an_identity(app, db):
    result = app.test_cli_runner().invoke(args=["installation", "show"])
    assert result.exit_code == 1
    assert "No installation identity found" in result.output
    assert all(_rows(t) == [] for t in IDENTITY_TABLES)
