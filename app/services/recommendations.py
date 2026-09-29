"""Deterministic menu recommendation engine.

**This engine is deterministic and does not use the Local Qwen model yet.**
No LLM, no external API, no network access, no embeddings service, no
vector database: TF-IDF over the menu's own text plus cosine similarity,
computed in-process with scikit-learn. The same inputs always produce the
same ranking.

How a recommendation is produced
--------------------------------
1. **Candidate set.** Available items in active categories only -- the same
   filter the public menu uses. An item a customer could not order is never
   a candidate.
2. **Dietary hard filter.** Applied *before* any scoring, so nothing
   downstream (similarity, order history) can reintroduce an item that
   violates a stated dietary restriction. See ``_DIETARY_COMPATIBILITY``.
3. **Feature text.** Each surviving item becomes one document built from
   its name, description, category, cuisine, spice level, dietary type and
   ingredients (``build_item_document``). Prices and database ids are
   deliberately excluded -- a price is a number whose *magnitude* matters,
   which TF-IDF cannot represent, and an id carries no taste information.
4. **Query text.** The customer's stated preferences become a document in
   the same vocabulary (``build_preference_document``). Optionally their
   completed order history becomes a second document.
5. **Preference match.** Each stated preference is compared with the
   item's own attribute (``preference_match``): cuisine exact, spice by
   distance on the ordinal scale, dietary exact-or-compatible. The match
   score is the mean over the preferences the customer actually set, and it
   is the number the page shows and the badge is derived from.
6. **Ranking.** Match score first, then TF-IDF cosine relevance (which also
   carries order history), then item name, then id -- so the output is
   stable and an item never outranks one that matches more of what the
   customer asked for.

Why two numbers
---------------
The TF-IDF cosine is computed over the whole item document, so it is
diluted by description length and weighted by how rare a token is on this
particular menu. Measured on the demo menu, an item matching all three
stated preferences scored 0.26 while one matching a single preference scored
0.62. That makes it a fine *relevance* signal for ordering near-equals, and
a misleading number to print next to the word "match". The page therefore
shows the preference match percentage, and the cosine stays internal.

The engine is **advisory only**. It returns real ``MenuItem`` rows from the
database; callers read the price off the model, never off anything computed
here. It never creates, modifies, or prices an order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models.category import Category
from app.models.customer_preference import CustomerPreference
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.models.menu_item import MenuItem
from app.models.order import Order, OrderItem, OrderStatus

# How much a customer's completed-order history counts relative to their
# explicitly stated preferences. Deliberately below 1.0: what someone *says*
# they want outranks what they happened to order before. History can only
# ever reorder items that already passed the dietary filter (step 2 above),
# so this weight can never surface a restricted item.
HISTORY_WEIGHT = 0.5

MAX_RECOMMENDATIONS = 10

# Which menu dietary types a customer holding a given dietary preference may
# be shown. This is a safety rule, not a ranking hint: it is a hard filter.
# `vegetarian` excludes `eggetarian` deliberately -- in the cuisine this menu
# targets, "vegetarian" conventionally excludes egg, and the stricter reading
# is the safe one to be wrong about.
_DIETARY_COMPATIBILITY: dict[DietaryType, frozenset[DietaryType]] = {
    DietaryType.VEGAN: frozenset({DietaryType.VEGAN}),
    DietaryType.VEGETARIAN: frozenset({DietaryType.VEGAN, DietaryType.VEGETARIAN}),
    DietaryType.EGGETARIAN: frozenset(
        {DietaryType.VEGAN, DietaryType.VEGETARIAN, DietaryType.EGGETARIAN}
    ),
    # Someone who eats meat has no restriction; every item stays a candidate.
    DietaryType.NON_VEGETARIAN: frozenset(DietaryType),
}


# Match thresholds, applied to the same whole-number percentage the page
# prints, so the badge and the number can never disagree at a boundary.
#
# Chosen from the values the match score can actually take (see
# preference_match): with three preferences set, all three exact is 100,
# two exact plus a near spice is 83, two exact plus one miss is 67, one exact
# plus one near plus one miss is 50, and one exact plus two misses is 33.
# "Fair" therefore means "at least half of what you asked for"; one out of
# three is a low match, not a fair one.
STRONG_MATCH_MIN = 80
GOOD_MATCH_MIN = 60
FAIR_MATCH_MIN = 40

# The model-facing label vocabulary. These four strings are what the frozen
# V4 adapter was trained on (data/, docs/AI.md), so they reach the prompt
# unchanged; the page may show a friendlier wording (display_label).
STRONG_MATCH = "Strong match"
GOOD_MATCH = "Good match"
FAIR_MATCH = "Fair match"
SUGGESTED = "Suggested"

# SpiceLevel is declared mildest-first, so declaration order is the scale.
_SPICE_SCALE = {level: index for index, level in enumerate(SpiceLevel)}


def classify_match(percent: int | None) -> str:
    """Map a displayed match percentage to its label.

    ``None`` (no preference stated, or an unusable score) is a general
    suggestion -- never a match claim the engine cannot support.
    """
    if percent is None:
        return SUGGESTED
    if percent >= STRONG_MATCH_MIN:
        return STRONG_MATCH
    if percent >= GOOD_MATCH_MIN:
        return GOOD_MATCH
    if percent >= FAIR_MATCH_MIN:
        return FAIR_MATCH
    return SUGGESTED


def cuisine_match(preferred: Cuisine | None, item_cuisine: Cuisine) -> float | None:
    """1.0 for the same cuisine, 0.0 otherwise; ``None`` when not stated.

    Cuisine is a controlled vocabulary, so equality is the exact similarity.
    A TF-IDF "cuisine profile" (all of a cuisine's dish text vs. an item's
    text) was evaluated and rejected: on the demo menu it rated Chicken
    Fajitas 31% similar to Indian and Steamed Rice 19% similar to Italian,
    purely through shared words such as "chicken", "onion" and "rice".
    """
    if preferred is None:
        return None
    return 1.0 if item_cuisine == preferred else 0.0


def spice_match(preferred: SpiceLevel | None, item_spice: SpiceLevel) -> float | None:
    """1.0 exact, 0.5 one step away on the scale, 0.0 further; ``None`` when not stated."""
    if preferred is None:
        return None
    distance = abs(_SPICE_SCALE[preferred] - _SPICE_SCALE[item_spice])
    return {0: 1.0, 1: 0.5}.get(distance, 0.0)


def dietary_match(preferred: DietaryType | None, item_dietary: DietaryType) -> float | None:
    """1.0 for the exact dietary type, 0.5 for a compatible but different one
    (e.g. a vegan dish for a vegetarian); ``None`` when not stated.

    Incompatible items never get here -- the hard filter removed them."""
    if preferred is None:
        return None
    return 1.0 if item_dietary == preferred else 0.5


@dataclass
class Recommendation:
    """One ranked result. ``menu_item`` is a real database row -- callers
    read the price from it, never from this object.

    ``score`` is the TF-IDF cosine relevance (a ranking tie-breaker, never
    shown as a percentage). ``match_score`` is the share of the customer's
    stated preferences this item meets, in [0, 1], or ``None`` when they
    stated none. The ``*_match`` fields are its per-preference components.
    """

    menu_item: MenuItem
    score: float
    match_score: float | None = None
    cuisine_match: float | None = None
    spice_match: float | None = None
    dietary_match: float | None = None

    @property
    def match_percent(self) -> int | None:
        """Whole-number percentage, rounded half-up. ``None`` for a missing
        or non-finite score, so nothing malformed can reach a badge."""
        if self.match_score is None or not math.isfinite(self.match_score):
            return None
        clamped = min(max(self.match_score, 0.0), 1.0)
        return int(math.floor(clamped * 100 + 0.5))

    @property
    def match_label(self) -> str:
        """Model-facing label -- always one of the four trained strings."""
        return classify_match(self.match_percent)

    @property
    def match_level(self) -> str:
        """Styling key: strong / good / fair / low / none."""
        if self.match_percent is None:
            return "none"
        return {STRONG_MATCH: "strong", GOOD_MATCH: "good", FAIR_MATCH: "fair"}.get(self.match_label, "low")

    @property
    def display_label(self) -> str:
        """Customer-facing wording: a below-threshold item is called a low
        match rather than hiding behind the neutral "Suggested"."""
        return "Low match" if self.match_level == "low" else self.match_label


def preference_match(preference: CustomerPreference | None, item: MenuItem) -> dict[str, float | None]:
    """Per-preference components and their mean, for one item."""
    components = {
        "cuisine_match": cuisine_match(preference.cuisine_preference if preference else None, item.cuisine),
        "spice_match": spice_match(preference.spice_preference if preference else None, item.spice_level),
        "dietary_match": dietary_match(
            preference.dietary_preference if preference else None, item.dietary_type
        ),
    }
    stated = [value for value in components.values() if value is not None]
    components["match_score"] = sum(stated) / len(stated) if stated else None
    return components


def build_item_document(item: MenuItem) -> str:
    """The text TF-IDF sees for one menu item.

    Structured attributes are repeated as plain tokens so they carry real
    weight next to free-text description prose. No price (magnitude is
    meaningless to TF-IDF) and no database id (carries no taste signal).
    """
    parts = [
        item.name,
        item.description or "",
        item.category.name if item.category else "",
        item.cuisine.value,
        f"spice_{item.spice_level.value}",
        f"diet_{item.dietary_type.value}",
        " ".join(ingredient.name for ingredient in item.ingredients),
    ]
    return " ".join(p for p in parts if p).lower()


def build_preference_document(preference: CustomerPreference | None) -> str:
    """The customer's stated taste, in the same vocabulary as the item
    documents above -- which is only possible because
    ``CustomerPreference`` reuses ``MenuItem``'s enums rather than defining
    parallel ones."""
    if preference is None:
        return ""
    parts = []
    if preference.cuisine_preference is not None:
        parts.append(preference.cuisine_preference.value)
    if preference.spice_preference is not None:
        parts.append(f"spice_{preference.spice_preference.value}")
    if preference.dietary_preference is not None:
        parts.append(f"diet_{preference.dietary_preference.value}")
    return " ".join(parts).lower()


def _allowed_dietary_types(preference: CustomerPreference | None) -> frozenset[DietaryType] | None:
    """``None`` means "no restriction stated" -- not "allow nothing"."""
    if preference is None or preference.dietary_preference is None:
        return None
    return _DIETARY_COMPATIBILITY.get(preference.dietary_preference, frozenset(DietaryType))


def candidate_items(preference: CustomerPreference | None) -> list[MenuItem]:
    """Available items in active categories, minus anything the customer's
    dietary restriction forbids.

    The availability filter is in the SQL; the dietary filter is applied on
    top. Both run before any scoring, so no later step can resurrect an
    excluded item.
    """
    items = (
        db.session.query(MenuItem)
        .join(Category)
        # build_item_document() reads item.category.name and item.ingredients
        # for every candidate. Without eager loading that is one extra query
        # per item (an N+1 measured at 14 statements for a 9-item menu in
        # M08); with it, the whole candidate set costs three.
        .options(joinedload(MenuItem.category), selectinload(MenuItem.ingredients))
        .filter(MenuItem.is_available.is_(True), Category.is_active.is_(True))
        .order_by(MenuItem.name)
        .all()
    )
    allowed = _allowed_dietary_types(preference)
    if allowed is None:
        return items
    return [item for item in items if item.dietary_type in allowed]


def build_history_document(user_id: int, allowed: frozenset[DietaryType] | None) -> str:
    """Text drawn from the customer's **completed** orders.

    Only ``COMPLETED`` orders count: a pending or cancelled order is not
    evidence that anyone enjoyed anything. Items whose dietary type is no
    longer compatible with the customer's current stated restriction are
    dropped here too -- so a customer who has since gone vegetarian is not
    nudged back toward meat by their own history. (The candidate filter
    already guarantees restricted items cannot be *returned*; this second
    check stops them from even influencing the ranking.)
    """
    rows = (
        db.session.query(MenuItem)
        .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
        .join(Order, Order.id == OrderItem.order_id)
        # Same eager-loading reason as candidate_items(): each row is turned
        # into a document that reads category and ingredients.
        .options(joinedload(MenuItem.category), selectinload(MenuItem.ingredients))
        .filter(Order.user_id == user_id, Order.status == OrderStatus.COMPLETED)
        .all()
    )
    if allowed is not None:
        rows = [item for item in rows if item.dietary_type in allowed]
    return " ".join(build_item_document(item) for item in rows)


def recommend_for_user(
    user_id: int,
    preference: CustomerPreference | None,
    *,
    limit: int = MAX_RECOMMENDATIONS,
    use_history: bool = True,
) -> list[Recommendation]:
    """Rank available menu items for one customer.

    Returns real ``MenuItem`` rows. An empty menu, an empty candidate set
    after the dietary filter, or a customer with no preferences and no
    history are all ordinary outcomes, not errors.
    """
    items = candidate_items(preference)
    if not items:
        return []

    preference_document = build_preference_document(preference)
    history_document = (
        build_history_document(user_id, _allowed_dietary_types(preference)) if use_history else ""
    )

    # Nothing to match on: return the candidates in a stable order rather
    # than an empty page. No match score renders as "Suggested", not as a
    # match claim the engine cannot support.
    if not preference_document and not history_document:
        return [Recommendation(menu_item=item, score=0.0) for item in items[:limit]]

    scores = _relevance_scores(items, preference_document, history_document)

    ranked = sorted(
        (
            Recommendation(menu_item=item, score=score, **preference_match(preference, item))
            for item, score in zip(items, scores)
        ),
        # Match first, relevance second; name then id as a deterministic
        # tie-break, so equal-scoring items never shuffle between requests.
        key=lambda r: (-(r.match_score or 0.0), -r.score, r.menu_item.name, r.menu_item.id),
    )
    return ranked[:limit]


def _relevance_scores(items: list[MenuItem], preference_document: str, history_document: str) -> list[float]:
    """TF-IDF cosine of the query (preferences + weighted history) against
    every item document. Always finite and in [0, 1]; all zeros when the
    corpus has no usable tokens instead of raising."""
    vectorizer = TfidfVectorizer(token_pattern=r"[a-z0-9_]+")
    try:
        item_matrix = vectorizer.fit_transform([build_item_document(item) for item in items])
    except ValueError:  # empty vocabulary: no document has a single token
        return [0.0] * len(items)

    query_vector = vectorizer.transform([preference_document]) if preference_document else None
    if history_document:
        history_vector = vectorizer.transform([history_document]) * HISTORY_WEIGHT
        query_vector = history_vector if query_vector is None else query_vector + history_vector

    # cosine_similarity yields 0 for an all-zero query (no known tokens).
    scores = np.nan_to_num(cosine_similarity(query_vector, item_matrix)[0])
    return [float(score) for score in np.clip(scores, 0.0, 1.0)]
