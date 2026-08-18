"""Production-shaped evaluation scenarios for M07.3 (V3 comparison).

Separate module from ``training/evaluate.py`` so the M07.1 harness and its
recorded results stay untouched as historical evidence.

Every scenario here is declared **structurally** -- preference triple, item
triple, match label -- and the prompt is rendered by the real production
function ``app.services.local_llm.build_prompt``. Expectations are then
*derived from the facts* rather than hand-written keyword lists, so the
scorer checks whether the model told the truth about this specific case
instead of whether it emitted a favoured phrase.

``None`` in a preference slot renders as "not set", exactly as production
does for a customer who never set that preference.

No scenario references price, ingredients, availability, order history,
calories or health: ``ExplanationRequest`` carries none of those, so a case
depending on them would test a situation the application cannot produce.
"""

from __future__ import annotations

# (name, category, (pref_cuisine, pref_dietary, pref_spice),
#  (item_name, item_cuisine, item_dietary, item_spice), match_label, note)
#
# Dish names are all absent from the v1/v2/v3 datasets and from their
# processed splits; ``assert_held_out`` re-verifies the full prompt text.

SCENARIOS = [
    # ---- A. FULL MATCH (2) ----
    ("full_match_indian", "A_full_match",
     ("indian", "vegetarian", "medium"),
     ("Methi Malai Mutter", "indian", "vegetarian", "medium"), "Strong match",
     "all three align; a confident match claim is correct here"),
    ("full_match_thai", "A_full_match",
     ("thai", "vegan", "hot"),
     ("Gaeng Som", "thai", "vegan", "hot"), "Strong match",
     "all three align"),

    # ---- B. MATCH WITH NOT SET (3) ----
    ("unset_dietary", "B_not_set",
     ("italian", None, "mild"),
     ("Risotto Milanese", "italian", "vegetarian", "mild"), "Strong match",
     "dietary unset: must not claim a dietary preference was met"),
    ("unset_cuisine_and_spice", "B_not_set",
     (None, "vegan", None),
     ("Nopales Salad", "mexican", "vegan", "medium"), "Good match",
     "only dietary set: must not claim cuisine or spice preferences"),
    ("unset_all", "B_not_set",
     (None, None, None),
     ("Potato Rosti", "continental", "vegetarian", "none"), "Suggested",
     "nothing set at all: must not claim any preference was matched"),

    # ---- C. CUISINE DIFFERENCE ONLY (2) ----
    ("cuisine_only_italian", "C_cuisine_only",
     ("indian", "vegan", "mild"),
     ("Panzanella", "italian", "vegan", "mild"), "Good match",
     "cuisine differs; dietary and spice both align -- must not become a dietary conflict"),
    ("cuisine_only_thai", "C_cuisine_only",
     ("chinese", "vegetarian", "medium"),
     ("Yam Woon Sen", "thai", "vegetarian", "medium"), "Good match",
     "cuisine differs only"),

    # ---- D. SPICE DIFFERENCE ONLY (2) ----
    ("spice_only_milder", "D_spice_only",
     ("indian", "vegetarian", "extra_hot"),
     ("Ker Sangri", "indian", "vegetarian", "mild"), "Good match",
     "spice differs only, item is milder"),
    ("spice_only_hotter", "D_spice_only",
     ("chinese", "vegan", "none"),
     ("Salt and Pepper Tofu", "chinese", "vegan", "hot"), "Good match",
     "spice differs only, item is hotter"),

    # ---- E. DIETARY COMPATIBILITY / SUBSUMPTION (2) ----
    ("dietary_vegan_for_vegetarian", "E_dietary_compat",
     ("continental", "vegetarian", "none"),
     ("Beetroot Carpaccio", "continental", "vegan", "none"), "Strong match",
     "vegan item suits a vegetarian customer -- NOT a mismatch"),
    ("dietary_veg_for_nonveg", "E_dietary_compat",
     ("italian", "non_vegetarian", "medium"),
     ("Orecchiette Broccoli", "italian", "vegetarian", "medium"), "Strong match",
     "vegetarian item is allowed for a non-vegetarian customer -- NOT a mismatch"),

    # ---- F. TWO-DIMENSION DIFFERENCE (2) ----
    ("two_dim_mexican_thai", "F_two_dim",
     ("mexican", "vegetarian", "extra_hot"),
     ("Mango Sticky Rice", "thai", "vegetarian", "none"), "Fair match",
     "cuisine AND spice differ; dietary aligns"),
    ("two_dim_thai_other", "F_two_dim",
     ("thai", "vegan", "extra_hot"),
     ("Moutabal Plate", "other", "vegan", "none"), "Fair match",
     "cuisine AND spice differ; dietary aligns"),

    # ---- G. SUGGESTED / LOW SIGNAL (1) ----
    ("suggested_low_signal", "G_suggested",
     ("indian", None, "hot"),
     ("Fattoush Salad", "other", "vegan", "none"), "Suggested",
     "nothing set lines up; must not manufacture a strong match"),

    # ---- H. GROUNDING (2) ----
    # Names chosen to tempt the model toward price/ingredient invention.
    ("grounding_premium_name", "H_grounding",
     ("continental", "non_vegetarian", "mild"),
     ("Osso Buco", "continental", "non_vegetarian", "mild"), "Strong match",
     "a premium-sounding dish: must not mention price, value or availability"),
    ("grounding_ingredient_name", "H_grounding",
     ("other", "vegan", "mild"),
     ("Manakish Zaatar", "other", "vegan", "mild"), "Strong match",
     "an ingredient-sounding dish: must not enumerate ingredients"),
]

# ---- Phase 8: contrastive groups ----
# Preference block held constant; only the candidate changes. V3 was trained
# on exactly this structure, so this is the sharpest test of whether it
# tracks the changed dimension.
CONTRASTIVE_GROUPS = [
    ("group_1_indian_veg_hot", ("indian", "vegetarian", "hot"), [
        ("g1_a_all_align", ("Undhiyu", "indian", "vegetarian", "hot"), "Strong match",
         "A: everything aligns"),
        ("g1_b_cuisine_differs", ("Harvest Grain Plate", "multi_cuisine", "vegetarian", "hot"), "Good match",
         "B: only cuisine differs"),
        ("g1_c_cuisine_and_spice", ("Soba Sesame Bowl", "multi_cuisine", "vegetarian", "none"), "Fair match",
         "C: cuisine and spice differ"),
    ]),
    ("group_2_chinese_vegan_medium", ("chinese", "vegan", "medium"), [
        ("g2_a_all_align", ("Jade Vegetable Stir Fry", "chinese", "vegan", "medium"), "Strong match",
         "A: everything aligns"),
        ("g2_b_spice_differs", ("Lotus Root Stir Fry", "chinese", "vegan", "extra_hot"), "Good match",
         "B: only spice differs"),
        ("g2_c_cuisine_and_spice", ("Koshari Bowl", "other", "vegan", "none"), "Fair match",
         "C: cuisine and spice differ"),
    ]),
    ("group_3_continental_egg_mild", ("continental", "eggetarian", "mild"), [
        ("g3_a_all_align", ("Spinach Frittata", "continental", "eggetarian", "mild"), "Strong match",
         "A: everything aligns"),
        ("g3_b_cuisine_differs", ("Menemen Skillet", "other", "eggetarian", "mild"), "Good match",
         "B: only cuisine differs"),
        ("g3_c_cuisine_and_spice", ("Poke Style Bowl", "multi_cuisine", "eggetarian", "extra_hot"), "Fair match",
         "C: cuisine and spice differ"),
    ]),
]


def all_cases() -> list[dict]:
    """Flatten scenarios + contrastive group members into one case list."""
    cases = []
    for name, category, pref, item, label, note in SCENARIOS:
        cases.append({
            "name": name, "category": category, "group": None,
            "pref": pref, "item": item, "label": label, "note": note,
        })
    for group_name, pref, members in CONTRASTIVE_GROUPS:
        for name, item, label, note in members:
            cases.append({
                "name": name, "category": "P8_contrastive", "group": group_name,
                "pref": pref, "item": item, "label": label, "note": note,
            })
    return cases
