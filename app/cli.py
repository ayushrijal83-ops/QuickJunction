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
"""

from __future__ import annotations

import sys

import click
from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

from app.services.auth import RegistrationError, RegistrationInput, create_admin
from app.services.installation import InstallationIdentityError, get_installation


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
