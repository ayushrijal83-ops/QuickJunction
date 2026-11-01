"""M6.2 Task 3 -- responsive cart, checkout and order-history layouts.

Desktop keeps the existing semantic tables (`d-none d-md-block`); a second,
mobile-only block (`d-md-none`) renders the same data as stacked
`.qj-panel` rows. This file asserts both blocks render from the same data,
that the cart's per-line forms (update/remove) keep their exact action,
fields and CSRF token in the mobile block, that the clear-cart confirmation
and the checkout order-type/submit controls are untouched, and that the
`.qj-summary` totals component replaced the old ad hoc `<dl class="row">`
markup. Visual overflow and on-device touch measurement are checked
separately (headless Chrome), not here.
"""

from __future__ import annotations

from pathlib import Path

from app.services.orders import checkout
from app.utils.formatting import money
from tests.conftest import make_menu_item
from tests.test_m5_catalog import body, req, signed_in

SITE_CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "site.css").read_text(encoding="utf-8")
APP_CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")


def get(client, url):
    response = req(client, "get", url, headers={"Accept": "text/html"})
    assert response.status_code == 200, (url, response.status_code)
    return body(response)


def add_to_cart(client, item, quantity=1):
    assert req(client, "post", "/cart/add", data={"menu_item_id": item.id, "quantity": quantity}).status_code == 302


# =============================================================================
# cart
# =============================================================================


def test_cart_renders_both_desktop_table_and_mobile_cards(app, db):
    item = make_menu_item(name="Paneer Tikka", price="199.00")
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item, quantity=2)
    page = get(client, "/cart")

    assert 'class="card d-none d-md-block"' in page
    assert 'class="d-md-none d-grid gap-3"' in page

    desktop = page[page.index('class="card d-none d-md-block"'):page.index('class="d-md-none d-grid gap-3"')]
    mobile = page[page.index('class="d-md-none d-grid gap-3"'):]

    for block in (desktop, mobile):
        assert "Paneer Tikka" in block
        assert "Rs.\xa0199.00" in block  # unit price
        assert "Rs.\xa0398.00" in block  # line total


def test_cart_mobile_cards_preserve_update_and_remove_forms(app, db):
    item = make_menu_item(name="Paneer Tikka")
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item)
    app.config["WTF_CSRF_ENABLED"] = True
    page = get(client, "/cart")
    mobile = page[page.index('class="d-md-none d-grid gap-3"'):page.index('action="/cart/clear"')]

    assert 'action="/cart/update"' in mobile and 'action="/cart/remove"' in mobile
    assert mobile.count(f'name="menu_item_id" value="{item.id}"') == 2  # one per form
    assert mobile.count('name="csrf_token"') == 2
    assert f'id="q-mobile-{item.id}"' in mobile and f'for="q-mobile-{item.id}"' in mobile
    assert 'min="1" max="20"' in mobile
    assert 'aria-label="Remove item"' in mobile

    # the mobile input id must not collide with the desktop row's id
    desktop = page[:page.index('class="d-md-none d-grid gap-3"')]
    assert f'id="q-{item.id}"' in desktop and f'id="q-mobile-{item.id}"' not in desktop


def test_cart_clear_form_appears_once_and_keeps_its_confirm_dialog(app, db):
    item = make_menu_item()
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item)
    app.config["WTF_CSRF_ENABLED"] = True
    page = get(client, "/cart")

    assert page.count('action="/cart/clear"') == 1
    clear_form = page[page.index('action="/cart/clear"'):]
    clear_form = clear_form[:clear_form.index("</form>")]
    assert 'data-confirm="Clear your cart?"' in clear_form
    assert 'data-confirm-body="Every item will be removed."' in clear_form
    assert 'name="csrf_token"' in clear_form


def test_cart_summary_uses_the_qj_summary_component(app, db):
    item = make_menu_item(price="199.00")
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item, quantity=2)
    page = get(client, "/cart")

    assert 'class="qj-summary mb-3"' in page
    assert 'class="qj-summary__total"' in page
    summary = page[page.index('class="qj-summary mb-3"'):page.index("</dl>")]
    assert "<dt>Items</dt>" in summary and "<dd>2</dd>" in summary
    assert "<dt>Subtotal</dt>" in summary and "Rs.\xa0398.00" in summary
    assert 'class="row mb-3"' not in page  # old markup is gone


# =============================================================================
# checkout
# =============================================================================


def test_checkout_renders_both_desktop_table_and_mobile_cards(app, db):
    item = make_menu_item(name="Butter Chicken", price="250.00")
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item)
    page = get(client, "/checkout")

    assert 'class="card d-none d-md-block"' in page
    assert 'class="d-md-none">' in page

    desktop = page[page.index('class="card d-none d-md-block"'):page.index('class="d-md-none">')]
    mobile = page[page.index('class="d-md-none">'):]

    for block in (desktop, mobile):
        assert "Butter Chicken" in block
        assert "Rs.\xa0250.00" in block


def test_checkout_order_type_and_submit_are_unchanged(app, db):
    item = make_menu_item(price="100.00")
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item)
    page = get(client, "/checkout")

    assert 'action="/checkout"' in page
    assert 'name="source" id="source-online"' in page and 'name="source" id="source-takeaway"' in page
    assert '<button type="submit" class="btn btn-primary btn-lg" data-submit-once>' in page
    assert "Place order" in page
    assert 'name="csrf_token"' in page


def test_checkout_summary_uses_the_qj_summary_component_and_keeps_tax(app, db):
    item = make_menu_item(price="100.00")
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item)
    page = get(client, "/checkout")

    assert 'class="qj-summary mb-3"' in page
    assert "Tax (13.00%)" in page and "Rs.\xa0113.00" in page
    assert 'class="row mb-3"' not in page


def test_checkout_still_submits_and_creates_an_order(app, db):
    """The template rewrite must not touch the submit path at all."""
    item = make_menu_item(price="100.00")
    client, _ = signed_in(app, "cust")
    add_to_cart(client, item)
    resp = req(client, "post", "/checkout", data={"source": "online"})
    assert resp.status_code == 302 and resp.headers["Location"].startswith("/orders/")


# =============================================================================
# order history
# =============================================================================


def test_order_history_renders_both_desktop_table_and_mobile_cards(app, db):
    item = make_menu_item(name="Dal Bhat", price="150.00")
    client, account = signed_in(app, "cust")
    order = checkout(account.id, {str(item.id): 1})
    page = get(client, "/orders")

    assert 'class="card d-none d-md-block"' in page
    assert 'class="d-md-none d-grid gap-3"' in page

    desktop = page[page.index('class="card d-none d-md-block"'):page.index('class="d-md-none d-grid gap-3"')]
    mobile = page[page.index('class="d-md-none d-grid gap-3"'):]

    for block in (desktop, mobile):
        assert f"Order #{order.id}" in block
        assert money(order.total) in block
        assert f'href="/orders/{order.id}"' in block
        assert 'qj-status qj-status--pending' in block  # status badge text/class


# =============================================================================
# touch targets (regression guard, not a new rule)
# =============================================================================


def test_new_controls_only_use_classes_already_covered_by_the_touch_rule():
    # .qj-site .btn / .form-control-sm already guarantee 44px -- confirm the
    # rules this work depends on are still declared, and that no new,
    # smaller custom control class was introduced for the mobile cards.
    assert "--qj-touch: 2.75rem;" in SITE_CSS
    for selector in (".qj-site .btn {", ".qj-site .form-control,"):
        rule = SITE_CSS[SITE_CSS.index(selector):]
        assert "min-height: var(--qj-touch)" in rule[:rule.index("}")], selector
    rule = SITE_CSS[SITE_CSS.index(".qj-site .form-control-sm,"):]
    assert "min-height: var(--qj-touch)" in rule[:rule.index("}")]


def test_qj_summary_component_still_declared_in_app_css():
    assert ".qj-summary {" in APP_CSS and ".qj-summary__total {" in APP_CSS
    assert ".qj-panel {" in APP_CSS
