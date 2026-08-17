"""The two request attributes every audit-logged route needs -- pulled out
once so app/routes/auth.py and app/routes/admin_menu.py don't each
reimplement it."""

from __future__ import annotations

from flask import request


def client_ip() -> str | None:
    return request.remote_addr


def user_agent() -> str | None:
    return request.headers.get("User-Agent")
