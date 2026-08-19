"""Seed a demonstration dataset.

    python scripts/seed_demo.py

Creates three accounts (customer / staff / admin), a small menu with a
deliberate mix of cuisines, spice levels and dietary types, and one
deactivated item so availability filtering is visible during a demo.

**Development convenience only.** It refuses to run against a production
configuration, and the passwords it creates are obvious placeholders that
would never satisfy a real deployment. It is idempotent: run it twice and
nothing is duplicated.

Orders are deliberately *not* seeded -- placing one through the UI is part of
the demo, and a seeded order would hide the checkout flow.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.category import Category  # noqa: E402
from app.models.enums import Cuisine, DietaryType, SpiceLevel  # noqa: E402
from app.models.menu_item import MenuItem  # noqa: E402
from app.models.user import Role, User  # noqa: E402
from app.services.menu import sync_menu_item_ingredients  # noqa: E402
from app.utils.security import hash_password  # noqa: E402

DEMO_PASSWORD = "demo-password-1"

ACCOUNTS = [
    ("customer", "customer@example.com", Role.CUSTOMER),
    ("staff", "staff@example.com", Role.STAFF),
    ("admin", "admin@example.com", Role.ADMIN),
]

CATEGORIES = [
    ("Starters", "Small plates to begin"),
    ("Mains", "Main courses"),
    ("Sides", "Rice, breads and salads"),
]

# (name, category, price, cuisine, spice, dietary, available, ingredients)
ITEMS = [
    ("Paneer Tikka", "Starters", "249.00", Cuisine.INDIAN, SpiceLevel.HOT,
     DietaryType.VEGETARIAN, True, "paneer, yogurt, chilli"),
    ("Chilli Paneer", "Starters", "269.00", Cuisine.CHINESE, SpiceLevel.HOT,
     DietaryType.VEGETARIAN, True, "paneer, chilli, soy"),
    ("Garden Salad", "Starters", "149.00", Cuisine.CONTINENTAL, SpiceLevel.NONE,
     DietaryType.VEGAN, True, "lettuce, cucumber, tomato"),
    ("Butter Chicken", "Mains", "349.00", Cuisine.INDIAN, SpiceLevel.MEDIUM,
     DietaryType.NON_VEGETARIAN, True, "chicken, butter, tomato"),
    ("Chana Masala", "Mains", "229.00", Cuisine.INDIAN, SpiceLevel.HOT,
     DietaryType.VEGAN, True, "chickpea, onion, tomato"),
    ("Margherita Pizza", "Mains", "399.00", Cuisine.ITALIAN, SpiceLevel.NONE,
     DietaryType.VEGETARIAN, True, "mozzarella, basil, tomato"),
    ("Thai Green Curry", "Mains", "379.00", Cuisine.THAI, SpiceLevel.EXTRA_HOT,
     DietaryType.NON_VEGETARIAN, True, "chicken, coconut, basil"),
    ("Egg Fried Rice", "Sides", "199.00", Cuisine.CHINESE, SpiceLevel.MILD,
     DietaryType.EGGETARIAN, True, "egg, rice, spring onion"),
    ("Steamed Rice", "Sides", "99.00", Cuisine.CHINESE, SpiceLevel.NONE,
     DietaryType.VEGAN, True, "rice"),
    # Deliberately unavailable: proves availability filtering in a demo.
    ("Sold Out Biryani", "Mains", "329.00", Cuisine.INDIAN, SpiceLevel.HOT,
     DietaryType.NON_VEGETARIAN, False, "rice, spice"),

    # --- Added for demonstration breadth -------------------------------
    # The original nine items left Mexican and multi-cuisine unrepresented
    # and clustered on hot/none, so several preference combinations all
    # returned the same dish and the recommender looked less capable than it
    # is. These fill the gaps in cuisine, dietary type and spice level so
    # that changing a preference visibly changes the recommendation.
    #
    # Demo data only. This has nothing to do with the training dataset in
    # data/, which is frozen at v4.
    ("Black Bean Tacos", "Mains", "319.00", Cuisine.MEXICAN, SpiceLevel.MEDIUM,
     DietaryType.VEGAN, True, "black bean, corn tortilla, coriander"),
    ("Chicken Fajitas", "Mains", "389.00", Cuisine.MEXICAN, SpiceLevel.MILD,
     DietaryType.NON_VEGETARIAN, True, "chicken, bell pepper, onion"),
    ("Som Tam Salad", "Starters", "239.00", Cuisine.THAI, SpiceLevel.EXTRA_HOT,
     DietaryType.VEGAN, True, "green papaya, peanut, lime"),
    ("Mushroom Risotto", "Mains", "409.00", Cuisine.ITALIAN, SpiceLevel.NONE,
     DietaryType.VEGETARIAN, True, "arborio rice, mushroom, parmesan"),
    ("Shepherd's Pie", "Mains", "419.00", Cuisine.CONTINENTAL, SpiceLevel.MILD,
     DietaryType.NON_VEGETARIAN, True, "lamb, potato, carrot"),
    ("Falafel Wrap", "Starters", "229.00", Cuisine.OTHER, SpiceLevel.MEDIUM,
     DietaryType.VEGAN, True, "chickpea, tahini, flatbread"),
    ("Shakshuka", "Starters", "259.00", Cuisine.OTHER, SpiceLevel.MILD,
     DietaryType.EGGETARIAN, True, "egg, tomato, pepper"),
    ("Buddha Bowl", "Mains", "349.00", Cuisine.MULTI_CUISINE, SpiceLevel.NONE,
     DietaryType.VEGAN, True, "quinoa, avocado, chickpea"),
]


def main() -> int:
    app = create_app()
    if app.config["ENV_NAME"] == "production":
        print("Refusing to seed demo data into a production configuration.", file=sys.stderr)
        return 1

    with app.app_context():
        categories = {}
        for name, description in CATEGORIES:
            category = db.session.query(Category).filter_by(name=name).first()
            if category is None:
                category = Category(name=name, description=description, is_active=True)
                db.session.add(category)
            categories[name] = category
        db.session.commit()

        created_items = 0
        for name, category_name, price, cuisine, spice, dietary, available, ingredients in ITEMS:
            item = db.session.query(MenuItem).filter_by(name=name).first()
            if item is None:
                item = MenuItem(
                    category_id=categories[category_name].id,
                    name=name,
                    description=f"House {name.lower()}.",
                    price=Decimal(price),
                    cuisine=cuisine,
                    spice_level=spice,
                    dietary_type=dietary,
                    is_available=available,
                )
                db.session.add(item)
                db.session.commit()
                created_items += 1
            sync_menu_item_ingredients(item, [i.strip() for i in ingredients.split(",")])

        created_users = 0
        for username, email, role in ACCOUNTS:
            if db.session.query(User).filter_by(username=username).first() is None:
                db.session.add(User(
                    username=username, email=email,
                    password_hash=hash_password(DEMO_PASSWORD),
                    role=role, is_active=True,
                ))
                created_users += 1
        db.session.commit()

        print(f"categories: {db.session.query(Category).count()}")
        print(f"menu items: {db.session.query(MenuItem).count()} (+{created_items} new)")
        print(f"users:      {db.session.query(User).count()} (+{created_users} new)")
        print()
        print("Demo accounts (development only):")
        for username, _, role in ACCOUNTS:
            print(f"  {username:<9} / {DEMO_PASSWORD}   role={role.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
