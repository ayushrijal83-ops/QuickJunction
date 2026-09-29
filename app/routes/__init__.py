"""HTTP layer: blueprints only.

Routes parse and validate the request, call a service, and shape the
response. Business rules belong in ``app.services``, persistence in
``app.models``.
"""

from __future__ import annotations

from flask import Flask

from app.routes.account import account_bp
from app.routes.admin_menu import admin_menu_bp
from app.routes.admin_settings import admin_settings_bp
from app.routes.admin_staff import admin_staff_bp
from app.routes.auth import auth_bp
from app.routes.cart import cart_bp
from app.routes.health import health_bp
from app.routes.inventory import inventory_bp
from app.routes.kitchen import kitchen_bp
from app.routes.main import main_bp
from app.routes.menu import menu_bp
from app.routes.orders import orders_bp
from app.routes.preferences import preferences_bp
from app.routes.reports import reports_bp
from app.routes.reservations import reservations_bp
from app.routes.staff_orders import staff_orders_bp
from app.routes.tables import tables_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(health_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(menu_bp)
    app.register_blueprint(admin_menu_bp)
    app.register_blueprint(admin_settings_bp)
    app.register_blueprint(admin_staff_bp)
    app.register_blueprint(cart_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(staff_orders_bp)
    app.register_blueprint(preferences_bp)
    app.register_blueprint(tables_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(kitchen_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(reservations_bp)
