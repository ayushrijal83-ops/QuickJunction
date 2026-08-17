"""Customer taste preferences and personalised recommendations.

Both routes are ``@login_required`` and both derive the customer identity
from the **server-side session** (``get_current_user()``), never from a
form field, query parameter, or header. There is no ``user_id`` parameter
anywhere in this module: a forged one has nothing to bind to, because the
only id that reaches the service layer is the authenticated one.

Recommendations are advisory. Prices rendered on the page are read from the
``MenuItem`` rows the engine returned -- the engine never produces a price,
and nothing here lets a customer influence one.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.forms import PreferenceForm
from app.services.preferences import PreferenceError, PreferenceInput, get_preferences, update_preferences
from app.services.local_llm import ExplanationRequest, generate_explanation
from app.services.recommendations import recommend_for_user
from app.utils.authorization import get_current_user, login_required

preferences_bp = Blueprint("preferences", __name__)


@preferences_bp.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences():
    user = get_current_user()
    existing = get_preferences(user.id)

    form = PreferenceForm() if request.method == "POST" else PreferenceForm(obj=existing)

    if form.validate_on_submit():
        try:
            update_preferences(
                user.id,
                PreferenceInput(
                    dietary_preference=form.dietary_preference.data,
                    cuisine_preference=form.cuisine_preference.data,
                    spice_preference=form.spice_preference.data,
                ),
            )
        except PreferenceError as exc:
            for field_name, messages in exc.errors.items():
                if hasattr(form, field_name):
                    getattr(form, field_name).errors.extend(messages)
                else:
                    for message in messages:
                        flash(message, "error")
        else:
            flash("Preferences saved.", "success")
            return redirect(url_for("preferences.recommendations"))

    return render_template("preferences/form.html", form=form)


@preferences_bp.get("/recommendations")
@login_required
def recommendations():
    user = get_current_user()
    preference = get_preferences(user.id)
    results = recommend_for_user(user.id, preference)
    return render_template(
        "preferences/recommendations.html",
        recommendations=results,
        preference=preference,
    )


@preferences_bp.get("/recommendations/explain")
@login_required
def explain():
    """Top recommendation, its deterministic facts, and -- if the local model
    is available -- a generated sentence about it.

    The deterministic half is rendered from the engine's own output and is
    complete on its own; the explanation is additive. ``generate_explanation``
    returns ``None`` rather than raising when the model is missing, slow, or
    broken, and the template shows a notice in that case. A separate page
    from ``/recommendations`` on purpose: CPU generation takes seconds, and
    the main recommendations page must stay fast.
    """
    user = get_current_user()
    preference = get_preferences(user.id)
    results = recommend_for_user(user.id, preference)
    top = results[0] if results else None

    explanation = None
    if top is not None:
        explanation = generate_explanation(
            ExplanationRequest(
                item_name=top.menu_item.name,
                item_cuisine=top.menu_item.cuisine.value,
                item_dietary=top.menu_item.dietary_type.value,
                item_spice=top.menu_item.spice_level.value,
                preferred_cuisine=preference.cuisine_preference.value
                if preference and preference.cuisine_preference else None,
                preferred_dietary=preference.dietary_preference.value
                if preference and preference.dietary_preference else None,
                preferred_spice=preference.spice_preference.value
                if preference and preference.spice_preference else None,
                match_label=top.match_label,
            )
        )

    return render_template(
        "preferences/explain.html",
        top=top,
        preference=preference,
        explanation=explanation,
    )
