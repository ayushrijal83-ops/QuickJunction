"""Centralised error handling.

Two audiences, one set of handlers.

**API clients** receive a short, generic JSON body. Stack traces, file paths,
SQL statements and configuration values stay on the server, in the log.

**Browsers** receive a styled HTML page extending ``base.html``, because a raw
JSON body rendered in a browser tab is not a product. The choice is made by
content negotiation, never by guessing from the URL.

The negotiation rule is deliberately conservative: HTML is served only when
the client says it *prefers* HTML over JSON. That keeps three cases correct:

===========================  ==================  ========
Client                       ``Accept``          Served
===========================  ==================  ========
Browser                      ``text/html`` 1.0,  HTML
                             ``*/*`` 0.8
``curl`` (no preference)     ``*/*`` (1.0 both)  JSON
API client / test client     absent or JSON      JSON
===========================  ==================  ========

An equal-quality match therefore falls through to JSON, which is why adding
HTML pages could not change the behaviour any existing API client or test
already depends on.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

_GENERIC_MESSAGE = "An internal error occurred."

# Status codes that have a dedicated template. Anything else falls back to the
# generic page so a rare 405 or 429 still renders as a product page.
_ERROR_TEMPLATES = {403: "errors/403.html", 404: "errors/404.html", 500: "errors/500.html"}


def wants_html() -> bool:
    """True when the caller prefers HTML over JSON.

    Strict ``>`` is load-bearing: with ``Accept: */*`` both media types score
    1.0, and with no ``Accept`` header at all both score 0. Neither is a
    browser navigating, so both correctly receive JSON.
    """
    accept = request.accept_mimetypes
    return accept["text/html"] > accept["application/json"]


def _json_error(status: int, message: str):
    return jsonify(error={"status": status, "message": message}), status


def _html_error(status: int, message: str):
    template = _ERROR_TEMPLATES.get(status, "errors/generic.html")
    return render_template(template, status=status, message=message), status


def render_error(status: int, message: str):
    """Render ``status`` in whichever form the caller asked for."""
    if wants_html():
        return _html_error(status, message)
    return _json_error(status, message)


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        # Werkzeug's own descriptions are safe: they never include internals.
        return render_error(exc.code or 500, exc.description or exc.name)

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # Full detail to the log, nothing but a generic message to the client.
        # The HTML page shows the same generic sentence -- switching format
        # must never widen what is disclosed.
        app.logger.exception("Unhandled exception: %s", type(exc).__name__)
        return render_error(500, _GENERIC_MESSAGE)
