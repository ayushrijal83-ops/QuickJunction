"""Milestone 06 tests: customer preferences and the deterministic
TF-IDF + cosine-similarity recommendation engine.

No LLM, no network: every assertion here runs against the in-process engine
and the test database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.models.customer_preference import CustomerPreference
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.models.menu_item import MenuItem
from app.models.order import OrderStatus
from app.models.user import Role
from app.services.orders import checkout
from app.services.preferences import (
    PreferenceError,
    PreferenceInput,
    get_preferences,
    update_preferences,
)
from app.services.recommendations import (
    build_item_document,
    build_preference_document,
    candidate_items,
    recommend_for_user,
)
from tests.conftest import make_category, make_menu_item, make_user

PASSWORD = "correct-horse-1"


def login(client, username: str):
    return client.post("/login", data={"username": username, "password": PASSWORD})


def make_and_login(client, username="cust", role=Role.CUSTOMER):
    user = make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)
    login(client, username)
    return user


def build_menu():
    """A small menu with enough variety to separate cuisine, spice and
    dietary signals from each other."""
    category = make_category(name="Mains")
    items = {
        "paneer": make_menu_item(
            category=category, name="Paneer Tikka", price="249.00",
            cuisine=Cuisine.INDIAN, spice_level=SpiceLevel.HOT, dietary_type=DietaryType.VEGETARIAN,
        ),
        "chicken": make_menu_item(
            category=category, name="Butter Chicken", price="349.00",
            cuisine=Cuisine.INDIAN, spice_level=SpiceLevel.HOT, dietary_type=DietaryType.NON_VEGETARIAN,
        ),
        "margherita": make_menu_item(
            category=category, name="Margherita Pizza", price="399.00",
            cuisine=Cuisine.ITALIAN, spice_level=SpiceLevel.NONE, dietary_type=DietaryType.VEGETARIAN,
        ),
        "salad": make_menu_item(
            category=category, name="Garden Salad", price="149.00",
            cuisine=Cuisine.CONTINENTAL, spice_level=SpiceLevel.NONE, dietary_type=DietaryType.VEGAN,
        ),
    }
    return category, items


def set_preference(user_id, dietary=None, cuisine=None, spice=None) -> CustomerPreference:
    preference = CustomerPreference(
        user_id=user_id, dietary_preference=dietary, cuisine_preference=cuisine, spice_preference=spice
    )
    _db.session.add(preference)
    _db.session.commit()
    return preference


def names(results) -> list[str]:
    return [r.menu_item.name for r in results]


# --- Preferences (1-4) --------------------------------------------------------


def test_1_preference_creation(client, db):
    user = make_user()
    assert get_preferences(user.id) is None

    preference = update_preferences(
        user.id,
        PreferenceInput(dietary_preference="vegetarian", cuisine_preference="indian", spice_preference="hot"),
    )
    assert preference.user_id == user.id
    assert preference.dietary_preference == DietaryType.VEGETARIAN
    assert preference.cuisine_preference == Cuisine.INDIAN
    assert preference.spice_preference == SpiceLevel.HOT
    assert _db.session.query(CustomerPreference).filter_by(user_id=user.id).count() == 1


def test_2_preference_update_reuses_the_same_row(client, db):
    user = make_user()
    update_preferences(user.id, PreferenceInput(cuisine_preference="indian"))
    update_preferences(user.id, PreferenceInput(cuisine_preference="italian", spice_preference="mild"))

    # One row per customer, not a new row per save.
    assert _db.session.query(CustomerPreference).filter_by(user_id=user.id).count() == 1
    preference = get_preferences(user.id)
    assert preference.cuisine_preference == Cuisine.ITALIAN
    assert preference.spice_preference == SpiceLevel.MILD
    # An omitted field is cleared, not silently retained.
    assert preference.dietary_preference is None


def test_3_invalid_preference_values_rejected(client, db):
    user = make_user()
    for payload in (
        PreferenceInput(dietary_preference="carnivore"),
        PreferenceInput(cuisine_preference="klingon"),
        PreferenceInput(spice_preference="volcanic"),
    ):
        with pytest.raises(PreferenceError):
            update_preferences(user.id, payload)

    # Nothing was written by any rejected attempt.
    assert get_preferences(user.id) is None

    # Empty string is valid: it means "no preference".
    preference = update_preferences(user.id, PreferenceInput(dietary_preference="", cuisine_preference=""))
    assert preference.dietary_preference is None
    assert preference.cuisine_preference is None


def test_3b_invalid_preference_rejected_over_http(client, db):
    make_and_login(client, "http_validator")
    resp = client.post("/preferences", data={"dietary_preference": "carnivore"})
    assert resp.status_code == 200  # re-rendered with errors, not redirected
    assert _db.session.query(CustomerPreference).count() == 0


def test_4_customer_can_only_modify_their_own_preferences(client, db):
    alice = make_user(username="alice_p", email="alice_p@example.com", password=PASSWORD)
    bob = make_user(username="bob_p", email="bob_p@example.com", password=PASSWORD)
    update_preferences(bob.id, PreferenceInput(cuisine_preference="thai"))

    login(client, "alice_p")
    # Alice posts, attempting to name Bob as the target in every way a
    # browser could: form field, query string, and header.
    resp = client.post(
        "/preferences?user_id=%d" % bob.id,
        data={"cuisine_preference": "italian", "user_id": str(bob.id), "id": str(bob.id)},
        headers={"X-User-Id": str(bob.id)},
    )
    assert resp.status_code == 302

    # Bob is untouched; Alice got her own row.
    assert get_preferences(bob.id).cuisine_preference == Cuisine.THAI
    assert get_preferences(alice.id).cuisine_preference == Cuisine.ITALIAN


# --- Feature representation and ranking (5-8) ---------------------------------


def test_5_tfidf_document_generation(client, db):
    category = make_category(name="Mains")
    item = make_menu_item(
        category=category, name="Paneer Tikka", price="249.00",
        cuisine=Cuisine.INDIAN, spice_level=SpiceLevel.HOT, dietary_type=DietaryType.VEGETARIAN,
    )
    document = build_item_document(item)

    for expected in ("paneer tikka", "mains", "indian", "spice_hot", "diet_vegetarian"):
        assert expected in document
    # Price and database id are deliberately absent.
    assert "249" not in document
    assert str(item.id) not in document.replace("spice_hot", "").replace("diet_vegetarian", "")

    assert build_preference_document(None) == ""
    preference = set_preference(make_user().id, dietary=DietaryType.VEGAN, cuisine=Cuisine.THAI)
    query = build_preference_document(preference)
    assert "thai" in query and "diet_vegan" in query


def test_6_cosine_similarity_ranks_the_closest_item_first(client, db):
    _, items = build_menu()
    user = make_user()
    preference = set_preference(user.id, cuisine=Cuisine.ITALIAN, spice=SpiceLevel.NONE)

    results = recommend_for_user(user.id, preference)
    assert results, "expected at least one recommendation"
    assert results[0].menu_item.name == "Margherita Pizza"
    # Scores are sorted descending.
    assert all(results[i].score >= results[i + 1].score for i in range(len(results) - 1))
    assert results[0].score > 0


def test_7_matching_cuisine_ranks_above_non_matching(client, db):
    _, items = build_menu()
    user = make_user()
    preference = set_preference(user.id, cuisine=Cuisine.INDIAN)

    ranked = names(recommend_for_user(user.id, preference))
    indian = [n for n in ranked if n in ("Paneer Tikka", "Butter Chicken")]
    others = [n for n in ranked if n in ("Margherita Pizza", "Garden Salad")]
    assert indian, "Indian items should be present"
    # Every Indian item outranks every non-Indian one.
    assert max(ranked.index(n) for n in indian) < min(ranked.index(n) for n in others)


def test_8_spice_preference_influences_ranking(client, db):
    _, items = build_menu()
    user = make_user()

    hot = names(recommend_for_user(user.id, set_preference(user.id, spice=SpiceLevel.HOT)))
    _db.session.query(CustomerPreference).delete()
    _db.session.commit()
    none = names(recommend_for_user(user.id, set_preference(user.id, spice=SpiceLevel.NONE)))

    # A hot preference puts a hot dish first; a no-spice preference does not.
    assert hot[0] in ("Paneer Tikka", "Butter Chicken")
    assert none[0] in ("Margherita Pizza", "Garden Salad")
    assert hot != none


# --- Safety rules (9-10) ------------------------------------------------------


def test_9_dietary_restriction_filtering(client, db):
    _, items = build_menu()
    user = make_user()

    vegetarian = names(recommend_for_user(user.id, set_preference(user.id, dietary=DietaryType.VEGETARIAN)))
    assert "Butter Chicken" not in vegetarian
    assert "Paneer Tikka" in vegetarian
    assert "Garden Salad" in vegetarian  # vegan is acceptable to a vegetarian

    _db.session.query(CustomerPreference).delete()
    _db.session.commit()
    vegan = names(recommend_for_user(user.id, set_preference(user.id, dietary=DietaryType.VEGAN)))
    assert vegan == ["Garden Salad"]  # only vegan items

    _db.session.query(CustomerPreference).delete()
    _db.session.commit()
    omnivore = names(recommend_for_user(user.id, set_preference(user.id, dietary=DietaryType.NON_VEGETARIAN)))
    assert "Butter Chicken" in omnivore  # no restriction


def test_10_unavailable_and_inactive_items_never_recommended(client, db):
    category, items = build_menu()
    make_menu_item(
        category=category, name="Sold Out Special", price="199.00",
        cuisine=Cuisine.INDIAN, spice_level=SpiceLevel.HOT,
        dietary_type=DietaryType.VEGETARIAN, is_available=False,
    )
    hidden_category = make_category(name="Retired", is_active=False)
    make_menu_item(
        category=hidden_category, name="Hidden Curry", price="199.00",
        cuisine=Cuisine.INDIAN, spice_level=SpiceLevel.HOT, dietary_type=DietaryType.VEGETARIAN,
    )

    user = make_user()
    preference = set_preference(user.id, cuisine=Cuisine.INDIAN, spice=SpiceLevel.HOT)

    ranked = names(recommend_for_user(user.id, preference))
    assert "Sold Out Special" not in ranked
    assert "Hidden Curry" not in ranked
    assert "Paneer Tikka" in ranked
    assert "Sold Out Special" not in [i.name for i in candidate_items(preference)]


# --- Edge cases (11-12) -------------------------------------------------------


def test_11_empty_preferences_still_return_available_items(client, db):
    _, items = build_menu()
    user = make_user()

    for preference in (None, set_preference(user.id)):  # no row at all, then an all-null row
        results = recommend_for_user(user.id, preference)
        assert len(results) == 4
        assert all(r.score == 0.0 for r in results)
        assert all(r.match_label == "Suggested" for r in results)


def test_12_no_menu_items_returns_empty_without_error(client, db):
    user = make_user()
    assert recommend_for_user(user.id, None) == []
    assert recommend_for_user(user.id, set_preference(user.id, cuisine=Cuisine.INDIAN)) == []

    # Also empty when the dietary filter removes every candidate.
    category = make_category(name="Meat Only")
    make_menu_item(
        category=category, name="Steak", price="799.00", cuisine=Cuisine.CONTINENTAL,
        spice_level=SpiceLevel.NONE, dietary_type=DietaryType.NON_VEGETARIAN,
    )
    _db.session.query(CustomerPreference).delete()
    _db.session.commit()
    assert recommend_for_user(user.id, set_preference(user.id, dietary=DietaryType.VEGAN)) == []


# --- Output integrity (13-15) -------------------------------------------------


def test_13_results_contain_real_database_ids(client, db):
    _, items = build_menu()
    user = make_user()
    results = recommend_for_user(user.id, set_preference(user.id, cuisine=Cuisine.INDIAN))

    real_ids = {i.id for i in _db.session.query(MenuItem).all()}
    for result in results:
        assert result.menu_item.id in real_ids
        assert _db.session.get(MenuItem, result.menu_item.id) is not None


def test_14_prices_come_from_the_database_untouched(client, db):
    _, items = build_menu()
    user = make_user()
    results = recommend_for_user(user.id, set_preference(user.id, cuisine=Cuisine.INDIAN))

    for result in results:
        stored = _db.session.get(MenuItem, result.menu_item.id)
        assert result.menu_item.price == stored.price
        assert isinstance(result.menu_item.price, Decimal)  # never float

    paneer = next(r for r in results if r.menu_item.name == "Paneer Tikka")
    assert paneer.menu_item.price == Decimal("249.00")


def test_15_engine_never_fabricates_menu_items(client, db):
    _, items = build_menu()
    user = make_user()
    results = recommend_for_user(user.id, set_preference(user.id, cuisine=Cuisine.INDIAN))

    stored_names = {i.name for i in _db.session.query(MenuItem).all()}
    assert {r.menu_item.name for r in results} <= stored_names
    assert len(results) <= _db.session.query(MenuItem).count()


def test_15b_rendered_page_shows_only_real_items_and_db_prices(client, db):
    _, items = build_menu()
    user = make_and_login(client, "render_cust")
    update_preferences(user.id, PreferenceInput(cuisine_preference="indian", dietary_preference="vegetarian"))

    body = client.get("/recommendations").get_data(as_text=True)
    assert "Paneer Tikka" in body
    assert "249.00" in body        # the database price
    assert "Butter Chicken" not in body  # excluded by dietary filter


# --- Authorization / CSRF (16-18) ---------------------------------------------


def test_16_recommendation_routes_require_authentication(client, db):
    build_menu()
    assert client.get("/recommendations").status_code == 401
    assert client.get("/preferences").status_code == 401
    assert client.post("/preferences", data={"cuisine_preference": "indian"}).status_code == 401
    assert _db.session.query(CustomerPreference).count() == 0


def test_17_csrf_protects_preference_changes(app, client, db):
    make_and_login(client, "csrf_cust")
    app.config["WTF_CSRF_ENABLED"] = True

    resp = client.post("/preferences", data={"cuisine_preference": "indian"})
    assert resp.status_code == 400
    assert _db.session.query(CustomerPreference).count() == 0


def test_18_forged_user_id_is_ignored(client, db):
    victim = make_user(username="victim_p", email="victim_p@example.com", password=PASSWORD)
    update_preferences(victim.id, PreferenceInput(cuisine_preference="thai"))
    attacker = make_and_login(client, "attacker_p")

    client.post(
        "/preferences",
        data={"cuisine_preference": "mexican", "user_id": str(victim.id), "customer_id": str(victim.id)},
        headers={"X-User-Id": str(victim.id), "X-Role": "admin"},
    )
    # The forged id changed nothing; the authenticated identity won.
    assert get_preferences(victim.id).cuisine_preference == Cuisine.THAI
    assert get_preferences(attacker.id).cuisine_preference == Cuisine.MEXICAN

    # The recommendations page is likewise scoped to the session identity.
    build_menu()
    body = client.get(f"/recommendations?user_id={victim.id}").get_data(as_text=True)
    assert "mexican" in body.lower()  # attacker's own stated preference


# --- Order history (20-21) ----------------------------------------------------


def _complete_order(user_id: int, item: MenuItem, quantity: int = 1):
    order = checkout(user_id, {str(item.id): quantity})
    order.status = OrderStatus.COMPLETED
    _db.session.commit()
    return order


def test_20_completed_order_history_influences_ranking(client, db):
    _, items = build_menu()
    user = make_user()

    # No preferences at all -- history is the only signal.
    baseline = recommend_for_user(user.id, None)
    assert all(r.score == 0.0 for r in baseline)

    _complete_order(user.id, items["margherita"])
    with_history = recommend_for_user(user.id, None)

    assert with_history[0].menu_item.name == "Margherita Pizza"
    assert with_history[0].score > 0
    # Turning history off returns to the neutral ordering.
    assert all(r.score == 0.0 for r in recommend_for_user(user.id, None, use_history=False))


def test_20b_only_completed_orders_count(client, db):
    _, items = build_menu()
    user = make_user()

    # A pending order is not evidence of enjoyment.
    checkout(user.id, {str(items["margherita"].id): 1})
    assert all(r.score == 0.0 for r in recommend_for_user(user.id, None))


def test_21_dietary_restriction_overrides_order_history(client, db):
    _, items = build_menu()
    user = make_user()

    # The customer used to order meat, repeatedly...
    _complete_order(user.id, items["chicken"], quantity=3)
    _complete_order(user.id, items["chicken"], quantity=2)

    # ...and has since declared themselves vegetarian.
    preference = set_preference(user.id, dietary=DietaryType.VEGETARIAN)
    ranked = names(recommend_for_user(user.id, preference))

    assert "Butter Chicken" not in ranked, "history must never override a dietary restriction"
    assert ranked, "vegetarian items should still be recommended"
    assert set(ranked) <= {"Paneer Tikka", "Margherita Pizza", "Garden Salad"}


def test_21b_history_of_restricted_items_does_not_skew_ranking(client, db):
    """The excluded meat dish is Indian and hot. A vegetarian customer whose
    history is entirely that dish must not have those attributes amplified
    on their behalf -- the history document drops restricted items too."""
    _, items = build_menu()
    user = make_user()
    _complete_order(user.id, items["chicken"], quantity=5)

    preference = set_preference(user.id, dietary=DietaryType.VEGAN)
    results = recommend_for_user(user.id, preference)
    assert names(results) == ["Garden Salad"]


# --- Determinism --------------------------------------------------------------


def test_22_engine_is_deterministic(client, db):
    _, items = build_menu()
    user = make_user()
    preference = set_preference(user.id, cuisine=Cuisine.INDIAN, spice=SpiceLevel.HOT)

    first = [(r.menu_item.id, round(r.score, 10)) for r in recommend_for_user(user.id, preference)]
    for _ in range(3):
        assert [(r.menu_item.id, round(r.score, 10)) for r in recommend_for_user(user.id, preference)] == first


# --- Query efficiency (M08) ---------------------------------------------------


def test_23_recommendations_do_not_issue_a_query_per_item(client, db):
    """M08 measured an N+1: build_item_document() reads `category` and
    `ingredients` for every candidate, which lazy-loaded one query per item
    (14 statements for a 9-item menu). candidate_items() now eager-loads
    both, so query count must stay flat as the menu grows."""
    from sqlalchemy import event
    from app.services.menu import sync_menu_item_ingredients

    category = make_category(name="Mains")
    user = make_user()
    for index in range(12):
        item = make_menu_item(category=category, name=f"Dish {index:02d}", price="100.00")
        sync_menu_item_ingredients(item, ["rice", "spice"])

    statements = []

    @event.listens_for(_db.engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        _db.session.expire_all()
        statements.clear()
        results = recommend_for_user(user.id, set_preference(user.id, cuisine=Cuisine.INDIAN))
    finally:
        event.remove(_db.engine, "before_cursor_execute", _count)

    # 12 items exist but the engine caps output at MAX_RECOMMENDATIONS; all
    # 12 are still scored, which is what would trigger the N+1.
    assert len(results) == 10
    # One for the candidate set, one for ingredients, one for history, plus a
    # little slack -- but nowhere near one per item.
    assert len(statements) <= 6, f"expected a flat query count, issued {len(statements)}"
