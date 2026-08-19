"""Session-backed authentication and role authorization.

The session holds two values and nothing else: a user id and a session
version. Flask signs it with ``SECRET_KEY`` (itsdangerous), so it is
tamper-evident but not encrypted; no role, permission or other trust
decision is ever read from the client, and neither stored value is secret.

Every request re-loads the user (and therefore the role) from the database,
which is what makes a server-side role change or deactivation take effect
on the user's very next request instead of waiting for their session to
expire.

**Session revocation (M09).** The version in the cookie is compared against
``User.session_version`` on every request, and ``logout_user`` increments
that column. This is what makes logout actually revoke: before M09 a copy of
the cookie taken before logout kept authenticating, because a stateless
signed cookie has nothing server-side to invalidate. See docs/SECURITY.md §6.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from flask import flash, g, jsonify, redirect, request, session, url_for
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.user import Role, User
from app.utils.errors import render_error, wants_html

logger = logging.getLogger(__name__)

_SESSION_KEY = "user_id"

# Session-version key. Short name because it rides in the cookie on every
# request; it holds an integer counter, never a secret (see User.session_version).
_VERSION_KEY = "sv"


def login_user(user: User) -> None:
    session.clear()  # drop any pre-authentication session state first
    session[_SESSION_KEY] = user.id
    # Stamp the session with the user's current revocation counter. Any later
    # logout increments the stored counter, which is what makes this session
    # (and every other one issued before that logout) stop validating.
    session[_VERSION_KEY] = user.session_version
    session.permanent = True
    g.current_user = user


def logout_user() -> None:
    """End the session and **revoke every session issued to this user so far.**

    Incrementing ``session_version`` is the server-side half of logout. Without
    it, ``session.clear()`` only clears the browser's own copy of the cookie,
    and a copy captured beforehand keeps authenticating until it expires --
    the vulnerability M08 found and M09 fixes.

    Scope is deliberately **all sessions for this user**, not just the current
    one. Distinguishing sessions would require per-session server-side state
    (a session table, or a token in the cookie); a single counter needs one
    integer column and no new subsystem. The trade-off is that logging out on
    one device logs the account out everywhere, which is the safe direction to
    err in and is documented in docs/SECURITY.md §6.
    """
    user = get_current_user()
    if user is not None:
        user.session_version = (user.session_version or 0) + 1
        try:
            db.session.commit()
        except SQLAlchemyError:
            # The cookie must still be cleared even if the counter cannot be
            # bumped, so the browser in hand is logged out regardless.
            db.session.rollback()
            logger.warning("Could not increment session_version on logout", exc_info=True)

    session.clear()
    g.current_user = None


def get_current_user() -> User | None:
    if "current_user" in g:
        return g.current_user

    user_id = session.get(_SESSION_KEY)
    user = None
    if user_id is not None:
        user = db.session.get(User, user_id)
        if user is None or not user.is_active:
            # Deleted or deactivated since the session was issued.
            session.clear()
            user = None
        elif session.get(_VERSION_KEY) != user.session_version:
            # Revoked by a later logout -- or issued before M09 added the
            # counter, in which case the session carries no version at all and
            # is likewise refused. Failing closed is intended: a session that
            # cannot prove it is current does not authenticate.
            session.clear()
            user = None

    g.current_user = user
    return user


def _unauthorized():
    """401 for API callers; a redirect to the login page for browsers.

    A browser that follows a link to a protected page should land on the login
    form with somewhere to go afterwards -- not on a JSON body. API clients
    still get 401, because a 302 to an HTML form is useless to them and would
    silently change the contract they already depend on.

    ``next`` carries the original path so login can return the user to it.
    ``request.full_path`` is a server-side value, and the login view validates
    it again before redirecting (see auth.safe_next_target).
    """
    if wants_html():
        flash("Please log in to continue.", "error")
        target = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.login", next=target))

    response = jsonify(error={"status": 401, "message": "Authentication required."})
    return response, 401


def _forbidden():
    message = "You do not have access to this resource."
    if wants_html():
        return render_error(403, message)

    response = jsonify(error={"status": 403, "message": message})
    return response, 403


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if get_current_user() is None:
            return _unauthorized()
        return view(*args, **kwargs)

    return wrapped


def require_role(*roles: Role) -> Callable[[Callable], Callable]:
    """Require an authenticated user whose role is one of ``roles``.

    Implies ``login_required`` -- an anonymous request gets 401, an
    authenticated request with the wrong role gets 403. The distinction
    matters to a client (log in vs. not permitted) and neither response
    reveals anything about resources the caller cannot access.
    """

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = get_current_user()
            if user is None:
                return _unauthorized()
            if user.role not in roles:
                return _forbidden()
            return view(*args, **kwargs)

        return wrapped

    return decorator
