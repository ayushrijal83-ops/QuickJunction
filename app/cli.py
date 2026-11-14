"""Operator commands (``flask <command>``).

``flask create-admin`` is the only supported way to create an administrator:
no HTTP route can (see app/services/auth.py). It is for creating the first
admin on a new installation, or another admin when none can sign in.

The password is only ever read from an interactive prompt with echo off --
never from a command-line argument (visible in ``ps`` and shell history) and
never from piped input -- and it is not printed or logged.

``flask installation show`` (O1) prints this installation's local
organization, branch and installation identity. It only reads; it never
creates or repairs an identity (see app/services/installation.py).

``flask organization members`` (O2) lists this organization's members, their
account role and status, their branch assignments, and any missing or
inconsistent relationship. It only reads; exit status 1 means it found an
inconsistency. Membership grants no access (see app/services/organization.py).
"""

from __future__ import annotations

import sys

import click
from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

from app.services.auth import RegistrationError, RegistrationInput, create_admin
from app.services.installation import InstallationIdentityError, get_installation
from app.services.organization import check_consistency, list_members


def _is_interactive() -> bool:
    """A real terminal on stdin. Separate so tests can stand one in."""
    return sys.stdin.isatty()


def register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    def create_admin_command() -> None:
        """Create an administrator account (prompts for every value)."""
        if not _is_interactive():
            # Without a terminal, getpass falls back to reading stdin in the
            # clear; refuse rather than accept a piped or empty password.
            raise click.ClickException(
                "create-admin must be run from an interactive terminal; it does not read "
                "passwords from piped input or arguments."
            )

        username = click.prompt("Username").strip()
        email = click.prompt("Email").strip()
        password = click.prompt("Password", hide_input=True)
        password_confirm = click.prompt("Confirm password", hide_input=True)

        try:
            user = create_admin(RegistrationInput(username=username, email=email,
                                                  password=password, password_confirm=password_confirm))
        except RegistrationError as exc:
            click.echo("Administrator NOT created:", err=True)
            for messages in exc.errors.values():
                for message in messages:
                    click.echo(f"  - {message}", err=True)
            sys.exit(1)
        except SQLAlchemyError as exc:
            # The service has rolled back. Only the error class is shown: a
            # SQLAlchemy message can embed statement parameters, which here
            # include the password hash.
            click.echo(
                f"Administrator NOT created: database error ({type(exc).__name__}). "
                "Check that the database is reachable and fully migrated (flask db upgrade).",
                err=True,
            )
            sys.exit(1)

        click.echo("Administrator created successfully.")
        click.echo(f"Username: {user.username}")

    @app.cli.group("installation")
    def installation_group() -> None:
        """This installation's local organization and branch identity."""

    @installation_group.command("show")
    def installation_show_command() -> None:
        """Show the installation identity (read-only)."""
        try:
            identity = get_installation()
        except InstallationIdentityError as exc:
            raise click.ClickException(str(exc))

        def hq(value: str | None) -> str:
            return value if value is not None else "not linked"

        click.echo(f"Organization:       {identity.organization_name}")
        click.echo(f"  Public ID:        {identity.organization_public_id}")
        click.echo(f"  HQ ID:            {hq(identity.organization_hq_id)}")
        click.echo(f"Branch:             {identity.branch_name}")
        click.echo(f"  Public ID:        {identity.branch_public_id}")
        click.echo(f"  HQ ID:            {hq(identity.branch_hq_id)}")
        click.echo(f"Installation ID:    {identity.installation_public_id}")
        click.echo(f"  Created:          {identity.installation_created_at:%Y-%m-%d %H:%M:%S}")
        click.echo(f"HQ linked:          {'yes' if identity.hq_linked else 'no'}")

    @app.cli.group("organization")
    def organization_group() -> None:
        """Organization membership and branch assignments (read-only)."""

    @organization_group.command("members")
    def organization_members_command() -> None:
        """List members, assignments and inconsistencies (read-only)."""
        from app.extensions import db
        from app.services.accounts import open_invitation_user_ids

        try:
            identity = get_installation()
            members = list_members()
            issues = check_consistency()
            invited = open_invitation_user_ids()
        except InstallationIdentityError as exc:
            raise click.ClickException(str(exc))
        finally:
            db.session.rollback()  # nothing was written; end the read transaction

        def account_state(m) -> str:
            if m.user_id in invited:
                return "invited"
            if not m.account_active:
                return "inactive"
            if m.role.value == "staff" and not m.staff_approved:
                return "pending approval"
            return "active"

        click.echo(f"Organization: {identity.organization_name}")
        click.echo(f"Branch:       {identity.branch_name}")
        click.echo(f"Members:      {len(members)}")
        click.echo("Membership does not grant access: sign-in and permissions follow the account role and status.")
        click.echo("Stored organization and branch roles grant nothing yet.")
        for m in members:
            click.echo("")
            click.echo(f"  {m.username}  (account: {m.role.value}, {account_state(m)})")
            org_role = m.organization_role.value if m.organization_role else "none"
            click.echo(f"    Membership:  {m.status.value} (organization role: {org_role})")
            if not m.assignments:
                click.echo("    Branches:    none")
            for a in m.assignments:
                branch_role = a.branch_role.value if a.branch_role else "none"
                click.echo(f"    Branch:      {a.branch_name} -- {a.status.value} (branch role: {branch_role})")
        click.echo("")
        if not issues:
            click.echo("Consistency:  OK")
            return
        click.echo(f"Consistency:  {len(issues)} issue(s)")
        for issue in issues:
            click.echo(f"  - [{issue.kind}] {issue.message}")
        sys.exit(1)
