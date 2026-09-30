"""Milestone 12: tax and discount pricing -- arithmetic, authorization,
snapshots, tampering, and the payment/refund/report integration."""

from __future__ import annotations

import itertools
import json
from decimal import Decimal

import pytest
import sqlalchemy as sa
from flask import g
from sqlalchemy.orm.attributes import set_committed_value

from app.extensions import db as _db
from app.models.audit_log import AuditEvent, AuditLog
from app.models.order import DiscountType, Order, OrderStatus
from app.models.pricing_settings import PricingSettings
from app.models.user import Role
from app.services.orders import OrderStatusError, update_order_status
from app.services.payments import PaymentError, record_payment, refund_payment
from app.services.pricing import PricingError, apply_discount, current_settings, price, update_settings
from app.services.sales import build_report
from app.utils.clock import db_today
from tests.conftest import csrf_token, make_user, rejected_by_check_constraint
from tests.test_restaurant_ops import customer, place, set_status

PASSWORD = "correct-horse-1"
D = Decimal


def member(name="cook", role=Role.STAFF):
    return make_user(username=name, email=f"{name}@example.com", password=PASSWORD, role=role)


def login(client, name):
    g.pop("current_user", None)  # known issue #29
    client.post("/login", data={"username": name, "password": PASSWORD})


def audit(event):
    return [json.loads(r.metadata_json) for r in _db.session.query(AuditLog).filter_by(event_type=event)]


_buyers = itertools.count()


def thousand():
    """An order with subtotal exactly 1000.00, taxed at the 13 % default.
    The first call's buyer is "diner" (tests log in as them); later calls get
    fresh buyers, since usernames are unique."""
    n = next(_buyers)
    return place(customer("diner" if n == 0 else f"buyer{n}"), price="500.00", qty=2)


@pytest.fixture(autouse=True)
def _reset_buyers():
    global _buyers
    _buyers = itertools.count()


# --- arithmetic ------------------------------------------------------------------


def test_checkout_snapshots_default_tax(db):
    order = thousand()
    assert current_settings() == (D("13.00"), D("20.00"))
    assert (order.subtotal, order.discount_amount, order.tax_rate, order.tax_amount, order.total) == (
        D("1000.00"), D("0.00"), D("13.00"), D("130.00"), D("1130.00"))


def test_percentage_discount(db):
    order = apply_discount(thousand(), "percent", "10", "loyal customer", member())
    # 1000 - 100 = 900; tax 13 % of 900 = 117; total 1017
    assert (order.discount_type, order.discount_value, order.discount_amount) == (DiscountType.PERCENT, D("10.00"), D("100.00"))
    assert (order.tax_amount, order.total, order.discount_reason) == (D("117.00"), D("1017.00"), "loyal customer")


def test_fixed_discount(db):
    order = apply_discount(thousand(), "fixed", "100", "voucher", member())
    assert (order.discount_amount, order.tax_amount, order.total) == (D("100.00"), D("117.00"), D("1017.00"))


def test_zero_discount_removes_it_and_needs_no_reason(db):
    cook = member()
    order = apply_discount(thousand(), "fixed", "100", "voucher", cook)
    order = apply_discount(order, "fixed", "0", "", cook)
    assert (order.discount_type, order.discount_reason, order.discount_amount, order.total) == (
        None, None, D("0.00"), D("1130.00"))


def test_rounding_is_half_up_to_the_cent(db):
    assert price(D("0.50"), D("0"), D("13.00")) == (D("0.07"), D("0.57"))   # 0.065 -> 0.07
    assert price(D("10.10"), D("0"), D("13.00")) == (D("1.31"), D("11.41"))  # 1.313 -> 1.31
    order = apply_discount(place(customer(), price="33.33", qty=1), "percent", "15", "promo", member())
    # 15 % of 33.33 = 4.9995 -> 5.00; taxable 28.33; tax 3.6829 -> 3.68
    assert (order.discount_amount, order.tax_amount, order.total) == (D("5.00"), D("3.68"), D("32.01"))


def test_staff_cap_and_admin_up_to_100(db):
    cook, boss = member(), member("boss", Role.ADMIN)
    apply_discount(thousand(), "percent", "20", "max staff", cook)          # exactly the cap
    with pytest.raises(PricingError):
        apply_discount(thousand(), "percent", "20.01", "too much", cook)
    with pytest.raises(PricingError):
        apply_discount(thousand(), "fixed", "200.01", "too much", cook)      # fixed measured as % too
    order = apply_discount(thousand(), "percent", "100", "comped", boss)
    assert (order.discount_amount, order.tax_amount, order.total) == (D("1000.00"), D("0.00"), D("0.00"))


@pytest.mark.parametrize("dtype, value", [("percent", "100.01"), ("fixed", "1000.01"), ("fixed", "-1"),
                                          ("percent", "abc"), ("percent", "NaN"), ("bogus", "5")])
def test_excessive_or_invalid_discounts_rejected(db, dtype, value):
    order = thousand()
    with pytest.raises(PricingError):
        apply_discount(order, dtype, value, "reason", member("boss", Role.ADMIN))
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).total == D("1130.00")


def test_reason_required_and_bounded(db):
    cook = member()
    with pytest.raises(PricingError):
        apply_discount(thousand(), "percent", "5", "   ", cook)
    with pytest.raises(PricingError):
        apply_discount(thousand(), "percent", "5", "x" * 256, cook)


def test_database_keeps_the_figures_consistent(db):
    order = thousand()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text("UPDATE orders SET total = 1.00 WHERE id = :i"), {"i": order.id})
        _db.session.flush()
    _db.session.rollback()
    with rejected_by_check_constraint():
        _db.session.execute(sa.text(
            "UPDATE orders SET discount_amount = subtotal + 1, total = total - subtotal - 1 WHERE id = :i"), {"i": order.id})
        _db.session.flush()
    _db.session.rollback()


# --- historical snapshots ------------------------------------------------------------


def test_setting_changes_never_touch_existing_orders(db):
    boss = member("boss", Role.ADMIN)
    old = thousand()
    update_settings("5", "10", boss)
    new = place(customer("diner2"), price="500.00", qty=2)
    _db.session.expire_all()
    old = _db.session.get(Order, old.id)
    assert (old.tax_rate, old.total, new.tax_rate, new.total) == (D("13.00"), D("1130.00"), D("5.00"), D("1050.00"))
    # a later discount on the old order re-prices at ITS rate (13 %), not today's 5 %
    old = apply_discount(old, "fixed", "100", "late", boss)
    assert (old.tax_amount, old.total) == (D("117.00"), D("1017.00"))


def test_menu_price_change_does_not_touch_totals(db):
    order = thousand()
    order.items[0].menu_item.price = D("999.00")
    _db.session.commit()
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).total == D("1130.00")


# --- settings -----------------------------------------------------------------------


@pytest.mark.parametrize("tax, cap", [("-1", "20"), ("50.01", "20"), ("13", "100.01"), ("abc", "20"), ("13", "-5")])
def test_settings_validated(db, tax, cap):
    with pytest.raises(PricingError):
        update_settings(tax, cap, member("boss", Role.ADMIN))
    assert current_settings() == (D("13.00"), D("20.00"))


def test_settings_page_admin_only_and_audited(app, client, db):
    assert client.get("/admin/settings").status_code == 401
    customer()
    login(client, "diner")
    assert client.get("/admin/settings").status_code == 403
    staff = app.test_client()
    member()
    login(staff, "cook")
    assert staff.post("/admin/settings", data={"tax_rate": "0", "staff_max_discount": "100"}).status_code == 403
    boss = app.test_client()
    member("boss", Role.ADMIN)
    login(boss, "boss")
    assert "13.00%" in boss.get("/admin/settings").get_data(as_text=True)
    boss.post("/admin/settings", data={"tax_rate": "12.5", "staff_max_discount": "15"})
    assert _db.session.get(PricingSettings, 1).tax_rate == D("12.50")
    assert audit(AuditEvent.PRICING_SETTINGS_CHANGED) == [{"from": ["13.00", "20.00"], "to": ["12.50", "15.00"]}]


# --- routes: authorization + tampering ------------------------------------------------


def test_discount_route_authorization_and_audit(app, client, db):
    order = thousand()
    assert client.post(f"/staff/orders/{order.id}/discount", data={}).status_code == 401
    login(client, "diner")
    r = client.post(f"/staff/orders/{order.id}/discount",
                    data={"discount_type": "percent", "discount_value": "50", "discount_reason": "me"})
    assert r.status_code == 403
    staff = app.test_client()
    member()
    login(staff, "cook")
    staff.post(f"/staff/orders/{order.id}/discount", data={
        "discount_type": "percent", "discount_value": "10", "discount_reason": "late food",
        # forged figures -- all must be ignored
        "discount_amount": "999", "tax_amount": "0", "total": "1.00", "discounted_by_id": "1"})
    _db.session.expire_all()
    order = _db.session.get(Order, order.id)
    assert (order.discount_amount, order.tax_amount, order.total) == (D("100.00"), D("117.00"), D("1017.00"))
    assert audit(AuditEvent.ORDER_DISCOUNT_APPLIED) == [
        {"order_id": order.id, "type": "percent", "value": "10.00", "amount": "100.00", "total": "1017.00"}]


def test_customer_checkout_cannot_self_discount(client, db):
    from tests.conftest import make_menu_item
    item = make_menu_item(price="100.00")
    customer()
    login(client, "diner")
    client.post("/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    client.post("/checkout", data={"discount_type": "fixed", "discount_value": "100", "discount_amount": "100",
                                   "tax_rate": "0", "tax_amount": "0", "total": "0"})
    order = _db.session.query(Order).one()
    assert (order.discount_amount, order.tax_rate, order.total) == (D("0.00"), D("13.00"), D("113.00"))


def test_discount_requires_csrf(app, client, db):
    order = thousand()
    member()
    login(client, "cook")
    app.config["WTF_CSRF_ENABLED"] = True
    client.post(f"/staff/orders/{order.id}/discount",
                data={"discount_type": "percent", "discount_value": "10", "discount_reason": "x"})
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).discount_amount == D("0.00")
    token = csrf_token(client, f"/staff/orders/{order.id}")
    client.post(f"/staff/orders/{order.id}/discount",
                data={"discount_type": "percent", "discount_value": "10", "discount_reason": "x", "csrf_token": token})
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).discount_amount == D("100.00")


# --- payments, refunds, cancellation ---------------------------------------------------


def test_payment_takes_the_discounted_taxed_total_and_locks_pricing(db):
    cook = member()
    order = apply_discount(thousand(), "percent", "10", "promo", cook)
    payment = record_payment(order, "card", cook)
    assert payment.amount == D("1017.00")
    with pytest.raises(PricingError):
        apply_discount(order, "percent", "15", "after payment", member("boss", Role.ADMIN))


def test_stale_discount_after_payment_is_refused(db):
    """The discounting request loaded the order before the payment committed."""
    cook = member()
    order = thousand()
    record_payment(order, "cash", cook)
    set_committed_value(order, "payment", None)
    with pytest.raises(PricingError):
        apply_discount(order, "fixed", "100", "stale", cook)
    _db.session.expire_all()
    assert _db.session.get(Order, order.id).total == D("1130.00")


def test_completed_or_cancelled_orders_cannot_be_discounted(db):
    cook = member()
    done, gone = thousand(), place(customer("other"), price="10.00", qty=1)
    set_status(done, OrderStatus.COMPLETED)
    update_order_status(gone, OrderStatus.CANCELLED)
    for order in (done, gone):
        with pytest.raises(PricingError):
            apply_discount(order, "percent", "5", "x", cook)


def test_refunds_still_bounded_by_the_actual_payment(db):
    cook = member()
    order = apply_discount(thousand(), "fixed", "100", "voucher", cook)
    payment = record_payment(order, "cash", cook)
    with pytest.raises(PaymentError):
        refund_payment(payment, "1017.01", None)
    refund_payment(payment, "1017.00", None)
    update_order_status(order, OrderStatus.CANCELLED)  # fully refunded -> cancellable
    assert order.status == OrderStatus.CANCELLED


# --- reporting ----------------------------------------------------------------------


def test_report_uses_snapshots_not_current_settings(db):
    boss = member("boss", Role.ADMIN)
    a = apply_discount(thousand(), "percent", "10", "promo", boss)        # 1000 - 100, tax 117
    b = place(customer("diner2"), price="200.00", qty=1)                  # 200, tax 26
    for o in (a, b):
        record_payment(o, "cash", boss)
        set_status(o, OrderStatus.COMPLETED)
    refund_payment(a.payment, "17.00", "cold")
    update_settings("0", "0", boss)  # must not change any reported figure

    today = db_today()
    t = build_report(today, today, "t").totals
    assert (t.gross, t.discounts, t.tax, t.refunds) == (D("1200.00"), D("100.00"), D("143.00"), D("17.00"))
    assert t.net == D("1083.00")                     # 1200 - 100 - 17; tax excluded
    assert t.collected == D("1017.00") + D("226.00") - D("17.00")
    assert (t.orders, t.paid) == (2, 2)


def test_order_pages_show_the_breakdown(app, client, db):
    order = apply_discount(thousand(), "percent", "10", "promo", member())
    login(client, "diner")
    body = client.get(f"/orders/{order.id}").get_data(as_text=True)
    assert "Discount" in body and "100.00" in body and "Tax (13.00%)" in body and "1,017.00" in body
    staff = app.test_client()
    login(staff, "cook")
    body = staff.get(f"/staff/orders/{order.id}").get_data(as_text=True)
    assert "&ldquo;promo&rdquo;" in body and "Tax (13.00%)" in body


def test_checkout_page_previews_tax(client, db):
    from tests.conftest import make_menu_item
    item = make_menu_item(price="100.00")
    customer()
    login(client, "diner")
    client.post("/cart/add", data={"menu_item_id": item.id, "quantity": 1})
    body = client.get("/checkout").get_data(as_text=True)
    assert "Tax (13.00%)" in body and "113.00" in body
