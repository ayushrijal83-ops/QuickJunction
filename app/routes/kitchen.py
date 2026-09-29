"""Kitchen screen (M14). STAFF and ADMIN only; read-only here -- its buttons
post to the existing ``/staff/orders/<id>/status`` route (CSRF, workflow
allow-list, atomic transition, audit) with ``return_to=kitchen``.

Privacy: the kitchen sees what it cooks (items, quantities, table/source,
timings), never who ordered it -- no username, email or other customer data.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from app.models.user import Role
from app.routes.forms import OrderStatusForm
from app.services.kitchen import KITCHEN_COLUMNS, KITCHEN_NEXT, minutes, queue, stats
from app.utils.authorization import require_role
from app.utils.clock import db_now

kitchen_bp = Blueprint("kitchen", __name__, url_prefix="/staff/kitchen")


@kitchen_bp.get("")
@require_role(Role.STAFF, Role.ADMIN)
def board():
    now = db_now()
    columns = queue()
    return render_template(
        "kitchen/board.html",
        columns=columns,
        column_order=KITCHEN_COLUMNS,
        next_status=KITCHEN_NEXT,
        stats=stats(columns, now.date()),
        now=now,
        minutes=minutes,
        form=OrderStatusForm(),
    )
