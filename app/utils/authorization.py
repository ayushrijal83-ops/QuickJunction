"""Session-backed authentication and role authorization.

The session holds nothing but a user id -- Flask signs it with
``SECRET_KEY`` (itsdangerous), so it is tamper-evident but not encrypted;
no role, permission or other trust decision is ever read from the client.
Every request re-loads the user (and therefore the role) from the database,
which is what makes a server-side role change or deactivation take effect
on the user's very next request instead of waiting for their session to
expire.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import abort, g, jsonify, session

from app.extensions import db
from app.models.user import Role, User

_SESSION_KEY = "user_id"


def login_user(user: User) -> None:
    session.clear()  # drop any pre-authentication session state first
    session[_SESSION_KEY] = user.id
    session.permanent = True
    g.current_user = user


def logout_user() -> None:
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

    g.current_user = user
    return user


def _unauthorized():
    response = jsonify(error={"status": 401, "message": "Authentication required."})
    return response, 401


def _forbidden():
    response = jsonify(error={"status": 403, "message": "You do not have access to this resource."})
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
