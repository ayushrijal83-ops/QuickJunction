"""Admin-only pricing settings: the tax rate and the staff discount cap.

GET shows the current values; POST (CSRF-protected app-wide) validates and
saves them through app/services/pricing.py and audits old -> new. Changing a
setting affects only orders placed/discounted afterwards -- every order keeps
its own snapshot.

M5 adds ``/admin/settings/restaurant``: the restaurant profile (name,
tagline, contact details, default menu language). ADMIN only, POST + CSRF,
no id in the URL or form -- there is exactly one profile -- and the changed
fields are audited (old -> new) in the same transaction.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.models.audit_log import AuditEvent
from app.models.user import Role
from app.routes.forms import PricingSettingsForm, RestaurantProfileForm
from app.services.audit import record_event
from app.services.pricing import PricingError, current_settings, update_settings
from app.services.restaurant import RestaurantError, RestaurantInput, get_restaurant, update_restaurant
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
            update_settings(form.tax_rate.data, form.staff_max_discount.data, actor,
                            audit=lambda change: record_event(
                                AuditEvent.PRICING_SETTINGS_CHANGED, success=True, user_id=actor.id,
                                ip_address=client_ip(), user_agent=user_agent(), metadata=change))
        except PricingError as exc:
            for messages in exc.errors.values():
                for message in messages:
                    flash(message, "error")
        else:
            flash("Pricing settings saved. They apply to new orders only.", "success")
            return redirect(url_for("admin_settings.pricing"))
    tax_rate, staff_max = current_settings()
    return render_template("admin/settings.html", form=form, tax_rate=tax_rate, staff_max=staff_max)


@admin_settings_bp.route("/restaurant", methods=["GET", "POST"])
@require_role(Role.ADMIN)
def restaurant():
    form = RestaurantProfileForm() if request.method == "POST" else RestaurantProfileForm(obj=get_restaurant())
    if form.validate_on_submit():
        actor = get_current_user()
        try:
            changed = update_restaurant(
                actor,
                RestaurantInput(name=form.name.data, tagline=form.tagline.data, address=form.address.data,
                                phone=form.phone.data, email=form.email.data, default_locale=form.default_locale.data),
                audit=lambda change: record_event(
                    AuditEvent.RESTAURANT_PROFILE_CHANGED, success=True, user_id=actor.id,
                    ip_address=client_ip(), user_agent=user_agent(), metadata={"changed": change}))
        except RestaurantError as exc:
            for field_name, messages in exc.errors.items():
                target = getattr(form, field_name, None)
                if target is not None and hasattr(target, "errors"):
                    target.errors = list(target.errors) + messages
                else:
                    for message in messages:
                        flash(message, "error")
        else:
            flash("Restaurant profile saved." if changed else "Nothing changed.", "success")
            return redirect(url_for("admin_settings.restaurant"))
    return render_template("admin/restaurant.html", form=form)
