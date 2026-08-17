"""Centralised error handling.

Clients receive a short, generic JSON body. Stack traces, file paths,
SQL statements and configuration values stay on the server, in the log.
"""

from __future__ import annotations

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

_GENERIC_MESSAGE = "An internal error occurred."


def _json_error(status: int, message: str):
    return jsonify(error={"status": status, "message": message}), status


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        # Werkzeug's own descriptions are safe: they never include internals.
        return _json_error(exc.code or 500, exc.description or exc.name)

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # Full detail to the log, nothing but a generic message to the client.
        app.logger.exception("Unhandled exception: %s", type(exc).__name__)
        return _json_error(500, _GENERIC_MESSAGE)
