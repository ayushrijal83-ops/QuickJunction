"""Decorative imagery -- the one place image files are chosen (final UI pass).

Presentation only: nothing here reads or changes business data beyond a menu
item's name, category and cuisine, which pick a photo. The photos are from
Unsplash (Unsplash License: free for commercial and non-commercial use, no
permission needed), resized to <=1600 px WebP and vendored under
``static/img/`` -- not hotlinked -- so the site stays offline-capable and the
CSP keeps ``img-src 'self'``. Sources are listed in ``static/img/CREDITS.md``.
"""

from __future__ import annotations

import re

from flask import Flask, url_for

#: Page-level photographs, by purpose.
SCENES = {
    "home": "hero-dining.webp",
    "auth": "hero-fine.webp",
    "reserve": "hero-interior.webp",
    "dashboard": "hero-spread.webp",
    "kitchen": "hero-chef.webp",
}

# First keyword found at a word start in "name category" wins; order matters
# (specific first). Word start, so "steak" never matches "tea".
_KEYWORDS = (
    (("butter chicken", "tikka masala", "korma", "paneer"), "curry-butter.webp"),
    (("salad",), "salad.webp"),
    (("green curry", "red curry", "thai", "pad thai"), "curry-thai.webp"),
    (("masala", "curry", "dal", "chana"), "curry-indian.webp"),
    (("pizza", "margherita"), "pizza.webp"),
    (("risotto",), "risotto.webp"),
    (("pasta", "spaghetti", "penne"), "pasta-salad.webp"),
    (("bowl", "quinoa"), "bowl.webp"),
    (("rice", "biryani", "pulao", "noodle"), "rice.webp"),
    (("burger", "sandwich"), "burger.webp"),
    (("taco", "fajita", "burrito", "nacho", "quesadilla", "wrap"), "mexican.webp"),
    (("tikka", "kebab", "skewer", "satay", "grill"), "skewers.webp"),
    (("coffee", "latte", "tea", "drink", "juice", "lassi", "shake", "beverage"), "coffee.webp"),
    (("dessert", "cake", "donut", "ice cream", "brownie", "sweet"), "dessert.webp"),
    (("fries", "fried", "wings", "nugget", "pakora", "samosa", "starter"), "fried.webp"),
    (("pie", "rib", "steak", "lamb", "roast"), "ribs.webp"),
)

_BY_CUISINE = {
    "indian": "curry-indian.webp",
    "chinese": "asian-bowls.webp",
    "thai": "curry-thai.webp",
    "italian": "pizza.webp",
    "mexican": "mexican.webp",
    "continental": "ribs.webp",
    "multi_cuisine": "bowl.webp",
}

_FALLBACK = "plate.webp"


def dish_file(name: str, category: str = "", cuisine: str = "") -> str:
    text = f"{name} {category}".lower()
    for words, filename in _KEYWORDS:
        if any(re.search("(?<![a-z])" + re.escape(w), text) for w in words):
            return filename
    return _BY_CUISINE.get(cuisine, _FALLBACK)


def dish_image(item) -> str:
    """URL of a representative photo for a MenuItem (or anything with the
    same ``name`` / ``category`` / ``cuisine`` attributes)."""
    category = getattr(getattr(item, "category", None), "name", "") or ""
    cuisine = getattr(getattr(item, "cuisine", None), "value", "") or ""
    return url_for("static", filename="img/" + dish_file(item.name, category, cuisine))


def scene_image(key: str) -> str:
    return url_for("static", filename="img/" + SCENES[key])


def register_imagery(app: Flask) -> None:
    app.jinja_env.globals.update(dish_image=dish_image, scene_image=scene_image)


if __name__ == "__main__":  # self-check: python -m app.utils.imagery
    assert dish_file("Butter Chicken") == "curry-butter.webp"
    assert dish_file("Paneer Tikka") == "curry-butter.webp"  # paneer before tikka
    assert dish_file("Chicken Tikka") == "skewers.webp"
    assert dish_file("Egg Fried Rice") == "rice.webp"
    assert dish_file("Som Tam Salad") == "salad.webp"
    assert dish_file("Thai Green Curry") == "curry-thai.webp"
    assert dish_file("Mystery", "Mains", "chinese") == "asian-bowls.webp"
    assert dish_file("Ribeye Steak") == "ribs.webp"  # not coffee via "tea"
    assert dish_file("Mystery") == _FALLBACK
    print("ok")
