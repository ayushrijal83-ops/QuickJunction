"""Read-only access to the local installation identity (O1).

``get_installation()`` returns the Organization, Branch and Installation
identity this database was given by migration 4c7e2a9f1b38, as an immutable
snapshot. It only ever reads: it never creates, repairs or replaces an
identity, and it raises ``InstallationIdentityError`` -- rather than guessing
-- if the records are missing, duplicated, mislinked or malformed. A broken
identity is something an operator must look at (normally by restoring a
backup), not something to paper over with a fresh UUID.

Nothing in the request path calls this yet; ``flask installation show``
(app/cli.py) is its only caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.installation import INSTALLATION_ID, Branch, Installation, Organization


class InstallationIdentityError(RuntimeError):
    """The identity is missing or invalid. The message is safe to print."""


@dataclass(frozen=True)
class InstallationIdentity:
    installation_public_id: str
    installation_created_at: datetime
    branch_public_id: str
    branch_name: str
    branch_hq_id: str | None
    organization_public_id: str
    organization_name: str
    organization_hq_id: str | None

    @property
    def hq_linked(self) -> bool:
        """True only once both the organization and the branch carry an
        HQ-issued id. Nothing in O1 sets one."""
        return self.organization_hq_id is not None and self.branch_hq_id is not None


def _is_canonical_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _read() -> InstallationIdentity:
    session = db.session
    installations = session.execute(sa.select(Installation)).scalars().all()
    if not installations:
        raise InstallationIdentityError(
            "No installation identity found. Run 'flask db upgrade' to create it; "
            "if this database was restored, the backup did not include it.")
    if len(installations) != 1 or installations[0].id != INSTALLATION_ID:
        raise InstallationIdentityError("The installation table must hold exactly one row with id 1.")
    installation = installations[0]

    organization_count = session.scalar(sa.select(sa.func.count()).select_from(Organization))
    branch_count = session.scalar(sa.select(sa.func.count()).select_from(Branch))
    if organization_count != 1 or branch_count != 1:
        raise InstallationIdentityError(
            f"Expected exactly one organization and one branch; found {organization_count} "
            f"and {branch_count}.")

    branch = session.get(Branch, installation.branch_id)
    if branch is None:
        raise InstallationIdentityError("The installation does not reference an existing branch.")
    organization = session.get(Organization, branch.organization_id)
    if organization is None:
        raise InstallationIdentityError("The branch does not reference an existing organization.")

    for label, value in (("installation", installation.public_id), ("branch", branch.public_id),
                         ("organization", organization.public_id)):
        if not _is_canonical_uuid(value):
            raise InstallationIdentityError(f"The {label} public id is not a valid UUID.")
    for label, value in (("branch", branch.name), ("organization", organization.name)):
        if not (value or "").strip():
            raise InstallationIdentityError(f"The {label} name is empty.")

    return InstallationIdentity(
        installation_public_id=installation.public_id,
        installation_created_at=installation.created_at,
        branch_public_id=branch.public_id,
        branch_name=branch.name,
        branch_hq_id=branch.hq_id,
        organization_public_id=organization.public_id,
        organization_name=organization.name,
        organization_hq_id=organization.hq_id,
    )


def get_installation() -> InstallationIdentity:
    try:
        return _read()
    except SQLAlchemyError as exc:
        # Only the error class: a driver message can embed connection details.
        db.session.rollback()
        raise InstallationIdentityError(
            f"The installation identity could not be read ({type(exc).__name__}). Check that the "
            "database is reachable and fully migrated (flask db upgrade).") from exc
