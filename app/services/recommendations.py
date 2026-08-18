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
5. **Ranking.** TF-IDF-vectorise the corpus, cosine-similarity the query
   against every candidate, sort descending. Ties break on item name so the
   output is stable.

The engine is **advisory only**. It returns real ``MenuItem`` rows from the
database; callers read the price off the model, never off anything computed
here. It never creates, modifies, or prices an order.
"""

from __future__ import annotations

from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models.category import Category
from app.models.customer_preference import CustomerPreference
from app.models.enums import DietaryType
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


@dataclass
class Recommendation:
    """One ranked result. ``menu_item`` is a real database row -- callers
    read the price from it, never from this object."""

    menu_item: MenuItem
    score: float

    @property
    def match_label(self) -> str:
        """Human-friendly indicator. Cosine similarity on short documents is
        not an intuitive number to show a customer, so the template shows
        this instead of (or alongside) the raw score."""
        if self.score >= 0.45:
            return "Strong match"
        if self.score >= 0.20:
            return "Good match"
        if self.score > 0.0:
            return "Fair match"
        return "Suggested"

    @property
    def score_percent(self) -> int:
        return int(round(self.score * 100))


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
    # than an empty page. Score 0.0 renders as "Suggested", not as a match
    # claim the engine cannot support.
    if not preference_document and not history_document:
        return [Recommendation(menu_item=item, score=0.0) for item in items[:limit]]

    documents = [build_item_document(item) for item in items]
    vectorizer = TfidfVectorizer(token_pattern=r"[a-z0-9_]+")
    item_matrix = vectorizer.fit_transform(documents)

    query_vector = vectorizer.transform([preference_document]) if preference_document else None
    if history_document:
        history_vector = vectorizer.transform([history_document]) * HISTORY_WEIGHT
        query_vector = history_vector if query_vector is None else query_vector + history_vector

    scores = cosine_similarity(query_vector, item_matrix)[0]

    ranked = sorted(
        (Recommendation(menu_item=item, score=float(score)) for item, score in zip(items, scores)),
        # Descending score; name ascending as a deterministic tie-break, so
        # equal-scoring items never shuffle between requests.
        key=lambda r: (-r.score, r.menu_item.name),
    )
    return ranked[:limit]
