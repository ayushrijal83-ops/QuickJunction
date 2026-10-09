"""M6.1 -- customer design system foundation (Obsidian + Champagne).

Presentation contracts: which shell each audience gets, that the customer
stylesheet stays out of operations, the phone navigation, the hero macro's
escaping, Nepali typography, and WCAG contrast for the declared tokens.
Behaviour (locale, CSRF, availability, orders) is covered by the existing
suites; this file only asserts what the design milestone owns.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models.enums import Locale
from app.models.user import Role
from tests.conftest import make_menu_item
from tests.test_m5_catalog import add_translation, body, req, seed_profile, signed_in

SITE_CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "site.css").read_text(encoding="utf-8")
HTML = {"Accept": "text/html"}

GUEST_PAGES = ["/", "/menu", "/cart", "/login", "/register"]
CUSTOMER_PAGES = ["/", "/account/", "/menu", "/cart", "/checkout", "/orders", "/reservations",
                  "/reservations/new", "/preferences", "/recommendations", "/account/password"]
OPS_PAGES = ["/account/", "/staff/kitchen", "/staff/orders", "/menu", "/cart", "/account/password"]
NEUTRAL_PAGES = ["/login/staff", "/login/admin", "/account/setup"]


def get(client, url):
    response = req(client, "get", url, headers=HTML)
    assert response.status_code == 200, (url, response.status_code)
    return body(response)


def is_site(page: str) -> bool:
    return bool(re.search(r'<body class="[^"]*\bqj-site\b', page)) and "css/site.css?v=" in page


def has_ops_theme(page: str) -> bool:
    return "js/theme.js" in page and "data-theme-toggle" in page and 'data-bs-theme="light"' in page


# =============================================================================
# shells
# =============================================================================


def test_guest_pages_get_the_customer_design_system(client, db):
    for url in GUEST_PAGES:
        page = get(client, url)
        assert is_site(page), url
        assert 'data-bs-theme="dark"' in page and "js/theme.js" not in page and "data-theme-toggle" not in page, url


def test_customer_pages_get_the_customer_design_system(app, db):
    make_menu_item()
    client, _ = signed_in(app, "cust")
    for url in CUSTOMER_PAGES:
        assert is_site(get(client, url)), url


@pytest.mark.parametrize("role", [Role.STAFF, Role.ADMIN])
def test_operations_pages_never_get_customer_styling(app, db, role):
    make_menu_item()
    client, _ = signed_in(app, "op", role)
    for url in OPS_PAGES + (["/admin/menu", "/admin/settings/restaurant"] if role == Role.ADMIN else []):
        page = get(client, url)
        assert not is_site(page) and "site.css" not in page and "qj-site" not in page, url
        assert has_ops_theme(page) and "qj-sidebar" in page, url  # operations shell and theme toggle intact
        assert "qj-bottombar" not in page and "qj-topnav" not in page, url


def test_staff_and_admin_sign_in_stay_neutral(client, db):
    for url in NEUTRAL_PAGES:
        page = get(client, url)
        assert not is_site(page) and "site.css" not in page, url
        assert has_ops_theme(page), url
        for customer_part in ("qj-topnav", "qj-bottombar", "qj-hero", "qj-footer", 'action="/locale"'):
            assert customer_part not in page, (url, customer_part)
    assert is_site(get(client, "/login"))  # the customer sign-in is a customer page


def test_stylesheets_are_cache_busted(client, db):
    page = get(client, "/menu")
    assert re.search(r'href="/static/css/app\.css\?v=\d+"', page)
    assert re.search(r'href="/static/css/site\.css\?v=\d+"', page)
    assert req(client, "get", "/static/css/site.css").status_code == 200


# =============================================================================
# phone navigation
# =============================================================================


def header(page: str) -> str:
    return page[page.index('<header class="qj-topnav'):page.index("</header>")]


def test_language_and_cart_stay_outside_the_collapsing_menu(app, client, db):
    for page in (get(client, "/menu"), get(signed_in(app, "cust")[0], "/account/")):
        bar = header(page)
        collapse = bar.index('class="collapse navbar-collapse"')
        assert bar.index('action="/locale"') < collapse
        assert bar.index('class="qj-topnav__cart"') < collapse
        assert bar.index('class="navbar-toggler"') < collapse
        assert bar.index('href="/menu"') > collapse  # page links are what collapses


def bottom_bar(page: str) -> str:
    start = page.index('<nav class="qj-bottombar')
    return page[start:page.index("</nav>", start)]


def test_bottom_bar_for_guests(client, db):
    bar = bottom_bar(get(client, "/menu"))
    assert "d-sm-none" in bottom_bar(get(client, "/menu"))  # phones only
    labels = re.findall(r"<span>([^<]+)</span>", bar)
    assert labels == ["Menu", "Cart", "Book", "Sign in"]
    assert 'href="/menu" aria-current="page"' in bar
    assert bar.count('href="/login"') == 2  # booking and account need a customer sign-in


def test_bottom_bar_for_customers_shows_active_state_and_cart_count(app, db):
    item = make_menu_item()
    client, _ = signed_in(app, "cust")
    req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": 3})
    bar = bottom_bar(get(client, "/cart"))
    assert re.findall(r"<span>([^<]+)</span>", bar) == ["Menu", "Cart", "Book", "Account"]
    assert 'href="/cart" aria-current="page"' in bar and 'href="/menu" aria-current' not in bar
    assert '<span class="qj-count">3<span class="visually-hidden"> items</span></span>' in bar
    assert 'href="/reservations/new"' in bar and 'href="/account/"' in bar


def test_bottom_bar_reserves_space_and_respects_safe_areas():
    assert "env(safe-area-inset-bottom)" in SITE_CSS
    assert "body.qj-site { padding-bottom: calc(var(--qj-bar-h) + env(safe-area-inset-bottom)); }" in SITE_CSS
    assert "min-height: var(--qj-bar-h);" in SITE_CSS and "--qj-bar-h: 3.5rem;" in SITE_CSS


def test_customer_pages_allow_safe_area_layout(client, db):
    assert "viewport-fit=cover" in get(client, "/menu")
    assert "viewport-fit=cover" not in get(client, "/login/staff")


def test_touch_targets_are_at_least_44px():
    assert "--qj-touch: 2.75rem;" in SITE_CSS  # 44px at the default 16px root
    for selector in (".qj-site .btn {", ".qj-site .qj-lang__btn {", ".qj-site .qj-pill {",
                     ".qj-site .qj-topnav .nav-link {", ".qj-site .form-control,"):
        rule = SITE_CSS[SITE_CSS.index(selector):]
        assert "min-height: var(--qj-touch)" in rule[:rule.index("}")], selector


# =============================================================================
# no glass, no inline script, no external URLs
# =============================================================================


def test_site_css_has_no_backdrop_blur_and_only_the_hero_scrim_gradient():
    assert "backdrop-filter" not in SITE_CSS
    gradients = [m.start() for m in re.finditer(r"gradient\(", SITE_CSS)]
    scrim = SITE_CSS.index(".qj-site .qj-hero::after {")
    scrim_end = SITE_CSS.index("}", scrim)
    assert gradients and all(scrim < g < scrim_end for g in gradients)


def test_customer_templates_no_longer_use_glass_classes(app, db):
    make_menu_item()
    client, _ = signed_in(app, "cust")
    for url in CUSTOMER_PAGES:
        page = get(client, url)
        assert not re.search(r'class="[^"]*\b(qj-glass|qj-glass-dark|btn-glass)\b', page), url


def test_customer_pages_have_no_inline_script_or_external_urls(app, client, db):
    make_menu_item()
    customer, _ = signed_in(app, "cust")
    pages = [get(client, u) for u in GUEST_PAGES] + [get(customer, u) for u in CUSTOMER_PAGES]
    for page in pages:
        assert all('src="' in tag for tag in re.findall(r"<script\b[^>]*>", page))
        assert not re.search(r"\son[a-z]+=", page)
        assert not re.search(r'(?:src|href|action)="(?:https?:)?//', page)
    assert not re.search(r"url\(\s*['\"]?(?:https?:)?//|@import", SITE_CSS)


# =============================================================================
# hero
# =============================================================================


def test_hero_escapes_restaurant_identity(client, db):
    seed_profile(name="<script>alert(1)</script>", tagline='"><img src=x onerror=alert(2)>',
                 address="<b>Lakeside</b>")
    page = get(client, "/")
    hero = page[page.index('<section class="qj-hero qj-hero--full"'):]
    hero = hero[:hero.index("</section>")]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in hero and "<script>alert" not in page
    assert "&lt;img src=x onerror=alert(2)&gt;" in hero and "<img src=x" not in page
    assert "&lt;b&gt;Lakeside&lt;/b&gt;" in hero


def test_home_leads_with_the_restaurant_and_its_food(client, db):
    seed_profile(name="Himalayan Test Kitchen", tagline="Mountain plates", phone="+977 61 000000")
    make_menu_item(name="Dal Bhat")
    page = get(client, "/")
    assert re.search(r'<h1 id="hero-title">Himalayan Test Kitchen</h1>', page)
    assert '<p class="qj-eyebrow mb-2">Mountain plates</p>' in page and "+977 61 000000" in page
    # food before the software's personalisation story
    assert page.index("Dal Bhat") < page.index("Made for you")
    assert page.index("Available today") < page.index("Personal suggestions")


def test_pages_using_the_hero_macro(app, db):
    client, _ = signed_in(app, "cust")
    for url, heading in (("/menu", 'id="menu-title">Discover something <em>delicious</em>.</h1>'),
                         ("/account/", '<em>cust</em>.</h1>'),
                         ("/reservations/new", '<h1 id="reserve-title">Book a table</h1>')):
        page = get(client, url)
        assert 'class="qj-hero qj-hero--band"' in page and heading in page, url


# =============================================================================
# menu, food cards and dish page
# =============================================================================


def test_food_card_keeps_its_form_and_states_price_and_category(app, client, db):
    app.config["WTF_CSRF_ENABLED"] = True
    item = make_menu_item(name="Paneer Tikka", price="249.50")
    page = get(client, "/menu")
    card = page[page.index('<article class="qj-food'):page.index("</article>")]
    assert 'action="/cart/add"' in card and f'name="menu_item_id" value="{item.id}"' in card
    assert 'name="csrf_token"' in card and 'name="quantity"' in card and 'min="1" max="20"' in card
    assert "Rs.\xa0249.50" in card and "Sold out" not in card  # money() uses a no-break space


def test_dish_page_states_availability_and_keeps_the_order_form(client, db):
    item = make_menu_item(name="Paneer Tikka")
    add_translation(item)
    page = get(client, f"/menu/{item.id}?lang=ne")
    assert "Available now" in page and 'action="/cart/add"' in page and 'class="qj-dish__price qj-price"' in page
    assert 'lang="ne"' in page and '<html lang="en"' in page
    assert 'style="' not in page[page.index('<main'):page.index("</main>")]


def test_unavailable_items_stay_off_the_public_menu(client, db):
    hidden = make_menu_item(name="Sold Out Soup", is_available=False)
    assert "Sold Out Soup" not in get(client, "/menu")
    assert req(client, "get", f"/menu/{hidden.id}", headers=HTML).status_code == 404


# =============================================================================
# typography, Nepali, accessibility
# =============================================================================


def test_nepali_typography_rules():
    assert ".qj-site :lang(ne) { letter-spacing: 0 !important; text-transform: none !important; }" in SITE_CSS
    body_rule = re.search(r"\.qj-site :where\(p, li, dd[^)]*\):lang\(ne\) \{ line-height: ([\d.]+); \}", SITE_CSS)
    assert body_rule and float(body_rule.group(1)) >= 1.6


def test_nepali_session_marks_translated_text_and_keeps_the_page_english(client, db):
    seed_profile(default_locale=Locale.NE)
    item = make_menu_item(name="Paneer Tikka")
    add_translation(item)
    page = get(client, "/menu")
    assert '<html lang="en"' in page and 'lang="ne">पनिर टिक्का</a>' in page


def test_focus_ring_reduced_motion_high_contrast_and_print_rules():
    assert ".qj-site :focus-visible { outline: 3px solid var(--qj-brand); outline-offset: 2px; }" in SITE_CSS
    assert "@media (prefers-reduced-motion: reduce)" in SITE_CSS
    assert "@media (forced-colors: active)" in SITE_CSS
    assert "@media print" in SITE_CSS


def test_skip_link_and_landmarks_survive(client, db):
    page = get(client, "/menu")
    assert 'class="visually-hidden-focusable qj-skip-link" href="#main"' in page and 'id="main"' in page
    assert 'aria-label="Main"' in page and 'aria-label="Quick actions"' in page
    assert 'aria-pressed="true"' in page and 'aria-label="Menu language"' in page


# =============================================================================
# WCAG contrast of the declared tokens
# =============================================================================


def _site_tokens() -> dict[str, str]:
    block = SITE_CSS[SITE_CSS.index(".qj-site {"):SITE_CSS.index("/* === Bootstrap mapping")]
    raw = dict(re.findall(r"--qj-([\w-]+):\s*([^;]+);", block))

    def resolve(value: str) -> str:
        match = re.fullmatch(r"var\(--qj-([\w-]+)\)", value.strip())
        return resolve(raw[match.group(1)]) if match else value.strip()

    return {name: resolve(value) for name, value in raw.items()}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


TOKENS = _site_tokens()

# Every text/background pair the customer pages use, at WCAG AA for normal text.
TEXT_PAIRS = [
    ("ink", "bg"), ("ink", "surface"), ("ink", "surface-2"),
    ("muted", "bg"), ("muted", "surface"), ("muted", "surface-2"),
    ("brand", "bg"), ("brand", "surface"), ("brand", "surface-2"),
    ("on-brand", "brand"), ("on-brand", "brand-dark"),
    ("accent", "bg"), ("accent", "surface"), ("on-accent", "accent"),
    ("sage", "surface"), ("danger", "surface"), ("danger", "bg"),
]


def test_palette_matches_the_locked_direction():
    assert (TOKENS["bg"], TOKENS["surface"], TOKENS["brand"], TOKENS["ink"], TOKENS["muted"], TOKENS["accent"]) == (
        "#0F0F0F", "#181818", "#E6C98A", "#F5F2EA", "#A7A29A", "#C96F4A")


@pytest.mark.parametrize("fg, bg", TEXT_PAIRS)
def test_token_pairs_meet_wcag_aa(fg, bg):
    assert contrast(TOKENS[fg], TOKENS[bg]) >= 4.5, (fg, bg, round(contrast(TOKENS[fg], TOKENS[bg]), 2))


def test_focus_ring_is_visible_against_every_surface():
    for surface in ("bg", "surface", "surface-2"):
        assert contrast(TOKENS["brand"], TOKENS[surface]) >= 3  # WCAG 1.4.11 non-text contrast


def test_ivory_on_terracotta_is_why_accent_fills_use_dark_text():
    assert contrast(TOKENS["ink"], TOKENS["accent"]) < 4.5
    assert TOKENS["on-accent"] == TOKENS["bg"]
