"""HTTP layer: blueprints only.

Routes parse and validate the request, call a service, and shape the
response. Business rules belong in ``app.services``, persistence in
``app.models``.
"""

from __future__ import annotations

from flask import Flask

from app.routes.account import account_bp
from app.routes.admin_menu import admin_menu_bp
from app.routes.auth import auth_bp
from app.routes.cart import cart_bp
from app.routes.health import health_bp
from app.routes.menu import menu_bp
from app.routes.orders import orders_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(health_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(menu_bp)
    app.register_blueprint(admin_menu_bp)
    app.register_blueprint(cart_bp)
    app.register_blueprint(orders_bp)
