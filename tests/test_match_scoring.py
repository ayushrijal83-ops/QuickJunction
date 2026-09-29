"""Phase 3: preference-match scoring, Good/Fair/Low classification and the
TF-IDF relevance helper.

Before Phase 3 the page printed the raw TF-IDF cosine as "% match" and
labelled every positive cosine "Fair match" -- measured on the demo menu, a
dish matching none of three stated preferences could show "Fair match 2%".
These tests pin the corrected behaviour against the real engine functions.
"""

from __future__ import annotations

import math

import pytest

from app.extensions import db as _db
from app.models.customer_preference import CustomerPreference
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.services import recommendations as rec_module
from app.services.recommendations import (
    FAIR_MATCH_MIN,
    GOOD_MATCH_MIN,
    STRONG_MATCH_MIN,
    Recommendation,
    _relevance_scores,
    classify_match,
    cuisine_match,
    dietary_match,
    recommend_for_user,
    spice_match,
)
from tests.conftest import make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"
TRAINED_LABELS = {"Strong match", "Good match", "Fair match", "Suggested"}


def set_preference(user_id, dietary=None, cuisine=None, spice=None) -> CustomerPreference:
    preference = CustomerPreference(
        user_id=user_id, dietary_preference=dietary, cuisine_preference=cuisine, spice_preference=spice
    )
    _db.session.add(preference)
    _db.session.commit()
    return preference


def rec_with(match_score) -> Recommendation:
    return Recommendation(menu_item=None, score=0.0, match_score=match_score)


# --- Classification boundaries ------------------------------------------------


@pytest.mark.parametrize(
    "percent, label",
    [
        (100, "Strong match"),
        (STRONG_MATCH_MIN, "Strong match"),
        (STRONG_MATCH_MIN - 1, "Good match"),
        (GOOD_MATCH_MIN, "Good match"),
        (GOOD_MATCH_MIN - 1, "Fair match"),
        (FAIR_MATCH_MIN, "Fair match"),
        (FAIR_MATCH_MIN - 1, "Suggested"),
        (1, "Suggested"),
        (0, "Suggested"),
        (None, "Suggested"),
    ],
)
def test_classification_boundaries(percent, label):
    assert classify_match(percent) == label


def test_thresholds_are_ordered():
    assert 0 < FAIR_MATCH_MIN < GOOD_MATCH_MIN < STRONG_MATCH_MIN <= 100


@pytest.mark.parametrize(
    "score, percent",
    [(0.3949, 39), (0.3951, 40), (0.5949, 59), (0.5951, 60), (0.7949, 79), (0.7951, 80), (0.0, 0), (1.0, 100)],
)
def test_rounding_boundaries_classify_on_the_displayed_percent(score, percent):
    r = rec_with(score)
    assert r.match_percent == percent
    assert r.match_label == classify_match(percent)


def test_badge_always_agrees_with_displayed_percent():
    for step in range(0, 1001):
        r = rec_with(step / 1000)
        assert r.match_label == classify_match(r.match_percent)
        if r.match_label == "Good match":
            assert r.match_percent >= GOOD_MATCH_MIN
        if r.match_label == "Fair match":
            assert r.match_percent >= FAIR_MATCH_MIN


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, -math.inf])
def test_missing_or_malformed_scores_never_claim_a_match(bad):
    r = rec_with(bad)
    assert r.match_percent is None
    assert r.match_label == "Suggested"
    assert r.match_level == "none"


def test_out_of_range_scores_are_clamped():
    assert rec_with(-0.2).match_percent == 0
    assert rec_with(1.7).match_percent == 100


def test_low_match_display_label_keeps_model_label_in_trained_vocabulary():
    low = rec_with(0.2)
    assert low.display_label == "Low match"
    assert low.match_label == "Suggested"  # what the V4 prompt receives


# --- Per-preference components ------------------------------------------------


def test_cuisine_component():
    assert cuisine_match(Cuisine.INDIAN, Cuisine.INDIAN) == 1.0
    assert cuisine_match(Cuisine.INDIAN, Cuisine.CHINESE) == 0.0
    assert cuisine_match(None, Cuisine.INDIAN) is None


def test_spice_component_uses_distance_on_the_scale():
    assert spice_match(SpiceLevel.HOT, SpiceLevel.HOT) == 1.0
    assert spice_match(SpiceLevel.HOT, SpiceLevel.MEDIUM) == 0.5
    assert spice_match(SpiceLevel.HOT, SpiceLevel.EXTRA_HOT) == 0.5
    assert spice_match(SpiceLevel.HOT, SpiceLevel.MILD) == 0.0
    assert spice_match(SpiceLevel.NONE, SpiceLevel.EXTRA_HOT) == 0.0
    assert spice_match(None, SpiceLevel.HOT) is None


def test_dietary_component():
    assert dietary_match(DietaryType.VEGETARIAN, DietaryType.VEGETARIAN) == 1.0
    assert dietary_match(DietaryType.VEGETARIAN, DietaryType.VEGAN) == 0.5
    assert dietary_match(None, DietaryType.VEGAN) is None


# --- Engine behaviour ---------------------------------------------------------


def build_menu():
    category = make_category(name="Mains")
    return {
        "paneer": make_menu_item(category=category, name="Paneer Tikka", cuisine=Cuisine.INDIAN,
                                 spice_level=SpiceLevel.HOT, dietary_type=DietaryType.VEGETARIAN),
        "chana": make_menu_item(category=category, name="Chana Masala", cuisine=Cuisine.INDIAN,
                                spice_level=SpiceLevel.MEDIUM, dietary_type=DietaryType.VEGAN),
        "pizza": make_menu_item(category=category, name="Margherita Pizza", cuisine=Cuisine.ITALIAN,
                                spice_level=SpiceLevel.NONE, dietary_type=DietaryType.VEGETARIAN),
        "salad": make_menu_item(category=category, name="Garden Salad", cuisine=Cuisine.CONTINENTAL,
                                spice_level=SpiceLevel.NONE, dietary_type=DietaryType.VEGAN),
    }


def by_name(results):
    return {r.menu_item.name: r for r in results}


def test_a_dish_matching_nothing_is_not_a_fair_match(client, db):
    """The reported bug: the pizza shares only the diet token with an
    Indian / extra-hot / vegetarian preference, had a positive cosine, and
    was shown as 'Fair match'. It meets one of three preferences."""
    build_menu()
    user = make_user()
    pref = set_preference(user.id, cuisine=Cuisine.INDIAN, spice=SpiceLevel.EXTRA_HOT,
                          dietary=DietaryType.VEGETARIAN)
    pizza = by_name(recommend_for_user(user.id, pref))["Margherita Pizza"]

    assert pizza.score > 0, "the old engine labelled any positive cosine a Fair match"
    assert pizza.match_percent == 33  # (0 cuisine + 0 spice + 1 diet) / 3
    assert pizza.match_label == "Suggested"
    assert pizza.display_label == "Low match"


def test_the_only_recommendation_is_not_promoted_to_fair(client, db):
    category = make_category(name="Mains")
    make_menu_item(category=category, name="Garden Salad", cuisine=Cuisine.CONTINENTAL,
                   spice_level=SpiceLevel.NONE, dietary_type=DietaryType.VEGAN)
    user = make_user()
    results = recommend_for_user(user.id, set_preference(user.id, cuisine=Cuisine.INDIAN, spice=SpiceLevel.HOT))
    assert len(results) == 1
    assert results[0].match_percent == 0
    assert results[0].match_label == "Suggested"


def test_components_and_percent_for_a_full_preference(client, db):
    build_menu()
    user = make_user()
    pref = set_preference(user.id, cuisine=Cuisine.INDIAN, spice=SpiceLevel.HOT, dietary=DietaryType.VEGETARIAN)
    results = by_name(recommend_for_user(user.id, pref))

    paneer = results["Paneer Tikka"]
    assert (paneer.cuisine_match, paneer.spice_match, paneer.dietary_match) == (1.0, 1.0, 1.0)
    assert paneer.match_percent == 100 and paneer.match_label == "Strong match"

    chana = results["Chana Masala"]  # indian, one spice step off, vegan (compatible)
    assert chana.match_percent == 67 and chana.match_label == "Good match"


def test_ranking_never_puts_a_weaker_match_above_a_stronger_one(client, db):
    build_menu()
    user = make_user()
    pref = set_preference(user.id, cuisine=Cuisine.INDIAN, spice=SpiceLevel.HOT, dietary=DietaryType.VEGETARIAN)
    results = recommend_for_user(user.id, pref)
    percents = [r.match_percent for r in results]
    assert percents == sorted(percents, reverse=True)
    assert results[0].menu_item.name == "Paneer Tikka"


def test_empty_preferences_make_no_match_claim(client, db):
    build_menu()
    user = make_user()
    for preference in (None, set_preference(user.id)):
        for r in recommend_for_user(user.id, preference):
            assert r.match_score is None and r.match_percent is None
            assert r.match_label == "Suggested"


def test_scores_are_deterministic_and_bounded(client, db):
    build_menu()
    user = make_user()
    pref = set_preference(user.id, cuisine=Cuisine.ITALIAN, spice=SpiceLevel.MILD)
    first = [(r.menu_item.id, r.match_score, r.score) for r in recommend_for_user(user.id, pref)]
    for _ in range(3):
        assert [(r.menu_item.id, r.match_score, r.score) for r in recommend_for_user(user.id, pref)] == first
    for _, match, relevance in first:
        assert 0.0 <= match <= 1.0 and 0.0 <= relevance <= 1.0


def test_duplicate_menu_entries_score_identically_and_order_stably(client, db):
    category = make_category(name="Mains")
    for suffix in ("A", "B"):
        make_menu_item(category=category, name=f"Paneer Tikka {suffix}", cuisine=Cuisine.INDIAN,
                       spice_level=SpiceLevel.HOT, dietary_type=DietaryType.VEGETARIAN)
    user = make_user()
    results = recommend_for_user(user.id, set_preference(user.id, cuisine=Cuisine.INDIAN))
    assert results[0].score == results[1].score
    assert results[0].match_score == results[1].match_score
    assert [r.menu_item.name for r in results] == ["Paneer Tikka A", "Paneer Tikka B"]


# --- TF-IDF relevance helper --------------------------------------------------


def test_tfidf_relevance_identical_related_unrelated(client, db):
    items = list(build_menu().values())
    scores = dict(zip((i.name for i in items), _relevance_scores(items, "paneer tikka indian spice_hot", "")))
    assert scores["Paneer Tikka"] > scores["Chana Masala"] > 0      # shares "indian"
    assert scores["Margherita Pizza"] == 0.0                         # shares nothing
    assert all(0.0 <= s <= 1.0 for s in scores.values())


def test_tfidf_relevance_zero_vector_query(client, db):
    items = list(build_menu().values())
    assert _relevance_scores(items, "tokens_nobody_uses", "") == [0.0] * len(items)


def test_tfidf_relevance_empty_vocabulary(client, db, monkeypatch):
    items = list(build_menu().values())
    monkeypatch.setattr(rec_module, "build_item_document", lambda item: "")
    assert _relevance_scores(items, "indian", "") == [0.0] * len(items)


def test_missing_cuisine_preference_is_not_scored(client, db):
    build_menu()
    user = make_user()
    for r in recommend_for_user(user.id, set_preference(user.id, spice=SpiceLevel.HOT)):
        assert r.cuisine_match is None
        assert r.spice_match is not None


# --- Rendering and the model boundary -----------------------------------------


def test_page_shows_the_same_percent_and_label(client, db):
    build_menu()
    make_user(username="shown", email="shown@example.com", password=PASSWORD)
    client.post("/login", data={"username": "shown", "password": PASSWORD})
    client.post("/preferences", data={"cuisine_preference": "indian", "spice_preference": "hot",
                                      "dietary_preference": "vegetarian"})
    body = client.get("/recommendations").get_data(as_text=True)
    assert "Preference match 100%" in body and "Strong match" in body
    assert "Preference match 17%" in body and "Low match" in body
    assert "Cuisine: matches" in body and "Spice: close" in body


def test_explain_passes_only_trained_labels_to_the_model(client, db, monkeypatch):
    import app.routes.preferences as routes

    seen = []
    monkeypatch.setattr(routes, "generate_explanation", lambda request: seen.append(request) or None)
    build_menu()
    make_user(username="explainer", email="explainer@example.com", password=PASSWORD)
    client.post("/login", data={"username": "explainer", "password": PASSWORD})
    for data in ({"cuisine_preference": "thai"}, {"spice_preference": "hot"}, {}):
        client.post("/preferences", data=data)
        assert client.get("/recommendations/explain").status_code == 200
    assert seen and all(r.match_label in TRAINED_LABELS for r in seen)
