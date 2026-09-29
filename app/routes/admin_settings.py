"""Admin-only pricing settings: the tax rate and the staff discount cap.

GET shows the current values; POST (CSRF-protected app-wide) validates and
saves them through app/services/pricing.py and audits old -> new. Changing a
setting affects only orders placed/discounted afterwards -- every order keeps
its own snapshot.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, url_for

from app.models.audit_log import AuditEvent
from app.models.user import Role
from app.routes.forms import PricingSettingsForm
from app.services.audit import record_event
from app.services.pricing import PricingError, current_settings, update_settings
from app.utils.authorization import get_current_user, require_role
from app.utils.request_meta import client_ip, user_agent

admin_settings_bp = Blueprint("admin_settings", __name__, url_prefix="/admin/settings")


@admin_settings_bp.route("", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def pricing():
    form = PricingSettingsForm()
    if form.validate_on_submit():
        actor = get_current_user()
        try:
            change = update_settings(form.tax_rate.data, form.staff_max_discount.data, actor)
        except PricingError as exc:
            for messages in exc.errors.values():
                for message in messages:
                    flash(message, "error")
        else:
            record_event(AuditEvent.PRICING_SETTINGS_CHANGED, success=True, user_id=actor.id,
                         ip_address=client_ip(), user_agent=user_agent(), metadata=change)
            flash("Pricing settings saved. They apply to new orders only.", "success")
            return redirect(url_for("admin_settings.pricing"))
    tax_rate, staff_max = current_settings()
    return render_template("admin/settings.html", form=form, tax_rate=tax_rate, staff_max=staff_max)
