"""Milestone 07.7 tests: the deterministic preference-safety guard.

The defect these guard against, measured across three model generations:

    v2: "matching your vegetarian requirement"          (dietary was not set)
    v3: "it is Mexican rather than the cuisine you set" (cuisine was not set)
    v3: "Fattoush Salad is not set and has no heat"     (placeholder leaked)
    v4: "It carries the medium heat you set"            (spice was not set)
    v4: "It is Continental and vegetarian, matching those you set."  (nothing set)

Three dataset iterations failed to remove this class, so M07.7 stopped trying
to teach it. The application already knows which preferences exist --
``ExplanationRequest.preferred_*`` is ``None`` when unset -- so the check is
deterministic and cannot regress with a model change.

No model is loaded here: ``explanation_is_preference_safe``,
``deterministic_explanation`` and ``apply_preference_safeguard`` are pure
functions of (text, request).
"""

from __future__ import annotations

import itertools
import re

import pytest

from app.services.local_llm import (
    ExplanationRequest,
    apply_preference_safeguard,
    deterministic_explanation,
    explanation_is_preference_safe,
)

CUISINES = ("indian", "chinese", "italian", "continental", "mexican", "thai",
            "multi_cuisine", "other")
DIETARY = ("vegetarian", "vegan", "eggetarian", "non_vegetarian")
SPICES = ("none", "mild", "medium", "hot", "extra_hot")

# app/services/recommendations.py::_DIETARY_COMPATIBILITY
COMPATIBLE = {
    "vegan": {"vegan"},
    "vegetarian": {"vegan", "vegetarian"},
    "eggetarian": {"eggetarian", "vegan", "vegetarian"},
    "non_vegetarian": {"vegetarian", "vegan", "eggetarian", "non_vegetarian"},
}


def request(*, cuisine=None, dietary=None, spice=None,
            item=("Test Dish", "indian", "vegetarian", "mild"),
            label="Good match") -> ExplanationRequest:
    name, item_cuisine, item_dietary, item_spice = item
    return ExplanationRequest(
        item_name=name, item_cuisine=item_cuisine, item_dietary=item_dietary,
        item_spice=item_spice, preferred_cuisine=cuisine, preferred_dietary=dietary,
        preferred_spice=spice, match_label=label)


# --- 1. Unset cuisine ---------------------------------------------------------

UNSET_CUISINE = request(dietary="vegetarian", spice="mild",
                        item=("Palak Paneer", "indian", "vegetarian", "mild"))


@pytest.mark.parametrize("text", [
    "Palak Paneer is vegetarian and mild, matching the cuisine you chose.",
    "Palak Paneer matches your cuisine preference.",
    "It is Indian, which is what you wanted.",
    "Palak Paneer is Indian as you asked, and it is vegetarian.",
    "This matches your preferred cuisine and your mild spice level.",
    "The cuisine you selected is well represented here.",
    "You asked for Indian, and this delivers.",
    "Palak Paneer suits your chosen cuisine.",
])
def test_1_unset_cuisine_claims_are_rejected(text):
    assert not explanation_is_preference_safe(text, UNSET_CUISINE), text


@pytest.mark.parametrize("text", [
    "Palak Paneer is vegetarian and mildly spiced, matching what you asked for.",
    "Palak Paneer is vegetarian as you prefer, and it is mild.",
    "It is Indian, vegetarian and mild.",                     # states, never credits
    "Palak Paneer is vegetarian and mild, which is what you set.",
])
def test_1b_unset_cuisine_correct_text_survives(text):
    """Describing the item's cuisine is fine; crediting the customer is not."""
    assert explanation_is_preference_safe(text, UNSET_CUISINE), text


# --- 2. Unset dietary ---------------------------------------------------------

UNSET_DIETARY = request(cuisine="indian", spice="mild",
                        item=("Aloo Paratha", "indian", "vegetarian", "mild"))


@pytest.mark.parametrize("text", [
    "Aloo Paratha is Indian and mild, matching your dietary preference.",
    "It meets the dietary requirement you selected.",
    "This is vegetarian, which is what you asked for.",
    "Aloo Paratha is vegetarian as you requested.",
    "It matches your vegetarian requirement.",
    "This suits your preferred diet.",
])
def test_2_unset_dietary_claims_are_rejected(text):
    assert not explanation_is_preference_safe(text, UNSET_DIETARY), text


@pytest.mark.parametrize("text", [
    "Aloo Paratha is Indian and mildly spiced, matching what you asked for.",
    "Aloo Paratha is Indian and mild as you prefer. It is vegetarian.",
])
def test_2b_unset_dietary_correct_text_survives(text):
    assert explanation_is_preference_safe(text, UNSET_DIETARY), text


# --- 3. Unset spice -----------------------------------------------------------

UNSET_SPICE = request(cuisine="indian", dietary="vegetarian",
                      item=("Chana Masala", "indian", "vegetarian", "hot"))


@pytest.mark.parametrize("text", [
    "Chana Masala is Indian and vegetarian, with the hot spice level you set.",
    "It carries the heat you asked for.",
    "This matches your spice preference.",
    "Chana Masala is hot, as you prefer.",
    "It is hot, which is what you wanted.",
    "This has your preferred spice level.",
])
def test_3_unset_spice_claims_are_rejected(text):
    assert not explanation_is_preference_safe(text, UNSET_SPICE), text


@pytest.mark.parametrize("text", [
    "Chana Masala is Indian and vegetarian, matching what you asked for.",
    "Chana Masala is Indian and vegetarian as you prefer. It is hot.",
])
def test_3b_unset_spice_correct_text_survives(text):
    assert explanation_is_preference_safe(text, UNSET_SPICE), text


# --- 4. All unset -------------------------------------------------------------

ALL_UNSET = request(item=("Potato Rosti", "continental", "vegetarian", "none"),
                    label="Suggested")


@pytest.mark.parametrize("text", [
    "Potato Rosti is Continental and vegetarian, matching your preferences.",
    "This matches what you asked for.",
    "It is vegetarian as you prefer.",
    "Potato Rosti suits your cuisine preference.",
    # The exact v4 failure: a correct denial followed by a false claim.
    "This is a general suggestion as you have not chosen any preferences. "
    "It is Continental and vegetarian, matching those you set.",
])
def test_4_all_unset_rejects_every_preference_claim(text):
    assert not explanation_is_preference_safe(text, ALL_UNSET), text


@pytest.mark.parametrize("text", [
    "You have not chosen any preferences, so this is a general suggestion.",
    "Potato Rosti is a vegetarian Continental dish with no heat. "
    "It is shown as a general suggestion.",
    "This item matches none of your preferences.",
    "Potato Rosti is Continental, vegetarian and unspiced.",
])
def test_4b_all_unset_correct_text_survives(text):
    assert explanation_is_preference_safe(text, ALL_UNSET), text


# --- 5. Placeholder leakage ---------------------------------------------------


@pytest.mark.parametrize("text,req", [
    ("Fattoush Salad is not set and has no heat.", ALL_UNSET),
    ("The cuisine is not set, so this is a suggestion.", UNSET_CUISINE),
    ("Dietary is not set for this item.", UNSET_DIETARY),
    ("Spice is not set.", UNSET_SPICE),
])
def test_5_placeholder_never_surfaces_as_prose(text, req):
    assert not explanation_is_preference_safe(text, req), text


@pytest.mark.parametrize("text", [
    # Found by running the real application in M07.7, not by the harness:
    # the model paraphrases the placeholder as the single word "unset".
    "Paneer Tikka is Indian and vegetarian as you prefer; its heat is hot rather than unset.",
    "Chana Masala is Indian, but the spice is unspecified.",
    "Its spice level: not set.",
    "The cuisine is not specified for this dish.",
])
def test_5a_placeholder_paraphrases_are_caught(text):
    """A one-word "unset" is the same defect as "not set" and must not ship."""
    assert not explanation_is_preference_safe(text, UNSET_SPICE), text


@pytest.mark.parametrize("text,req", [
    ("You have not set any preferences, so this is a general suggestion.", ALL_UNSET),
    ("You left the spice level unset.", UNSET_SPICE),
    ("Chana Masala is Indian and vegetarian. Cuisine was left unset.", UNSET_CUISINE),
])
def test_5b_truthful_statements_about_the_customer_are_not_leakage(text, req):
    """"You have not set a spice preference" is true English, not a leak.

    The difference from a leak is whose property is described: the customer's
    account, not the dish.
    """
    assert explanation_is_preference_safe(text, req), text


# --- 6. Every partial combination --------------------------------------------


@pytest.mark.parametrize("set_dims", [
    ("cuisine",), ("dietary",), ("spice",),                       # one set, two unset
    ("cuisine", "dietary"), ("cuisine", "spice"), ("dietary", "spice"),  # two set
])
def test_6_guard_protects_exactly_the_unset_dimensions(set_dims):
    """Only unset dimensions are policed; set ones may be discussed freely."""
    item = ("Test Dish", "indian", "vegetarian", "hot")
    kwargs = {"cuisine": "indian" if "cuisine" in set_dims else None,
              "dietary": "vegetarian" if "dietary" in set_dims else None,
              "spice": "hot" if "spice" in set_dims else None}
    req = request(item=item, **kwargs)

    claims = {
        "cuisine": "It is Indian, which is what you wanted.",
        "dietary": "It is vegetarian, which is what you wanted.",
        "spice": "It is hot, which is what you wanted.",
    }
    for dimension, text in claims.items():
        safe = explanation_is_preference_safe(text, req)
        if dimension in set_dims:
            assert safe, f"{dimension} IS set; the claim is true and must survive: {text}"
        else:
            assert not safe, f"{dimension} is unset; the claim is false and must be caught: {text}"


# --- 7. The replacement is safe, grounded and within the contract -------------


def test_7_rejected_text_is_replaced_not_edited():
    bad = "Nopales Salad is vegan, though it is Mexican rather than the cuisine you chose."
    req = request(dietary="vegan", item=("Nopales Salad", "mexican", "vegan", "medium"))
    result = apply_preference_safeguard(bad, req)
    assert result != bad
    assert "the cuisine you chose" not in result
    # A replacement, not a mutilated version of the original.
    assert explanation_is_preference_safe(result, req)


def test_7b_safe_text_is_returned_untouched():
    good = "Nopales Salad is vegan, matching what you asked for."
    req = request(dietary="vegan", item=("Nopales Salad", "mexican", "vegan", "medium"))
    assert apply_preference_safeguard(good, req) is good


def test_7c_none_passes_through():
    assert apply_preference_safeguard(None, ALL_UNSET) is None


def test_7d_fallback_is_always_safe_concise_and_grounded():
    """Exhaustive over every reachable preference/item combination.

    A fallback that failed its own check would loop, and one that ran long
    would break the two-sentence contract the UI renders against.
    """
    forbidden = re.compile(
        r"\b(price|cost|rupee|calorie|ingredient|contains|made with|available|"
        r"in stock|sold out|healthy|nutritious|protein|you ordered|previously|"
        r"popular|review)\b", re.I)
    checked = 0
    for cuisine, dietary, spice in itertools.product(
            CUISINES + (None,), DIETARY + (None,), SPICES + (None,)):
        for item in (("Test Dish", "indian", "vegetarian", "medium"),
                     ("Test Dish", "other", "vegan", "none"),
                     ("Test Dish", "multi_cuisine", "eggetarian", "extra_hot"),
                     ("Test Dish", "italian", "non_vegetarian", "hot")):
            if dietary is not None and item[2] not in COMPATIBLE[dietary]:
                continue  # the engine's filter would have removed it
            req = request(cuisine=cuisine, dietary=dietary, spice=spice, item=item)
            text = deterministic_explanation(req)
            checked += 1

            assert explanation_is_preference_safe(text, req), text
            assert not forbidden.search(text), f"fallback invented a fact: {text}"
            assert len(text) <= 400, text
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
            assert 1 <= len(sentences) <= 2, f"{len(sentences)} sentences: {text}"
            assert text[0].isupper() and text.rstrip()[-1] in ".!?", text
    assert checked > 700, f"only {checked} combinations exercised"


def test_7e_fallback_never_mentions_an_unset_dimension_value():
    """The whole point: an unset dimension is not discussed at all."""
    req = request(dietary="vegan", item=("Nopales Salad", "mexican", "vegan", "medium"))
    text = deterministic_explanation(req).lower()
    assert "mexican" not in text, text     # cuisine unset
    assert "medium" not in text, text      # spice unset
    assert "vegan" in text                 # dietary set -- may be discussed


# --- 8. The production path applies the guard --------------------------------


def test_8_generate_explanation_routes_through_the_safeguard():
    """A future refactor that bypassed the guard would silently reintroduce
    the defect, so the wiring itself is pinned."""
    import inspect

    from app.services import local_llm

    source = inspect.getsource(local_llm.generate_explanation)
    assert "apply_preference_safeguard" in source, \
        "generate_explanation no longer applies the preference safeguard"
