"""Quick Junction application factory.

Assembly order matters: configuration is resolved and validated before any
extension or handler is attached, so a bad environment fails at startup
rather than at the first request.
"""

from __future__ import annotations

from flask import Flask

from app.extensions import csrf, db, migrate
from app.routes import register_blueprints
from app.utils.authorization import get_current_user
from app.utils.errors import register_error_handlers
from app.utils.logging import configure_logging
from config import BaseConfig, get_config

__all__ = ["create_app"]


def create_app(config_name: str | None = None) -> Flask:
    """Build a configured Flask application.

    Args:
        config_name: ``development``, ``testing`` or ``production``.
            Defaults to the ``APP_ENV`` environment variable.
    """
    config_class: type[BaseConfig] = get_config(config_name)

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)
    config_class.validate()

    configure_logging(app)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    # Import for the side effect of registering models on db.metadata, which
    # is what `flask db migrate` inspects.
    from app import models  # noqa: F401

    register_error_handlers(app)
    register_blueprints(app)

    # So a template can show/hide admin-only controls without every route
    # passing the user in explicitly. Still just a display convenience --
    # every route re-checks authorization itself (see app/utils/authorization.py).
    #
    # cart_count drives the navbar badge. It counts quantities in the signed
    # session only -- it touches no database row and grants no authority, so
    # a tampered cookie can at worst show a wrong number on a badge.
    def _template_globals() -> dict:
        from app.utils.cart import get_cart

        total = 0
        for quantity in get_cart().values():
            try:
                total += int(quantity)
            except (TypeError, ValueError):
                continue
        return {"current_user": get_current_user(), "cart_count": total}

    app.context_processor(_template_globals)

    return app
