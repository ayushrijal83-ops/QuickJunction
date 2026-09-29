"""Phase 2B tests: browser error pages, currency formatting, registration flow.

Three demo-polish changes, each with a behaviour worth pinning:

* **Content-negotiated errors.** A browser gets a styled HTML page; an API
  client still gets the JSON body it already depends on. The negotiation is
  the interesting part, because getting it wrong silently changes the contract
  for every existing consumer.
* **Currency.** Money is ``Decimal`` end to end, and the display filter must
  not be the place a float sneaks in.
* **Registration.** Signing the new account in must not weaken validation,
  duplicate rejection, or role assignment.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.user import Role, User
from app.utils.formatting import CURRENCY_SYMBOL, MISSING, money
from tests.conftest import make_category, make_menu_item, make_user

BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
API = {"Accept": "application/json"}

PASSWORD = "correct-horse-1"


# --- 1. Content negotiation ---------------------------------------------------


def test_1_browser_404_is_html(client, db):
    response = client.get("/no-such-page", headers=BROWSER)
    assert response.status_code == 404
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    assert "We could not find that page" in body
    assert "Quick Junction" in body          # extends base.html


def test_1b_api_404_is_still_json(client, db):
    response = client.get("/no-such-page", headers=API)
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["status"] == 404


def test_1c_no_accept_header_stays_json(client, db):
    """The default for tools and the test client must not change.

    ``*/*`` and an absent header both score HTML and JSON equally, and equal
    is deliberately not enough to switch format -- otherwise adding HTML pages
    would have silently altered every existing API response.
    """
    assert client.get("/no-such-page").mimetype == "application/json"
    assert client.get("/no-such-page", headers={"Accept": "*/*"}).mimetype == "application/json"


def test_1d_browser_403_is_html(client, db):
    make_user(username="plaincust", email="plaincust@example.com",
              password=PASSWORD, role=Role.CUSTOMER)
    client.post("/login", data={"username": "plaincust", "password": PASSWORD})

    response = client.get("/admin/menu", headers=BROWSER)
    assert response.status_code == 403
    assert response.mimetype == "text/html"
    assert "do not have access" in response.get_data(as_text=True)


def test_1e_api_403_is_still_json(client, db):
    make_user(username="apicust", email="apicust@example.com",
              password=PASSWORD, role=Role.CUSTOMER)
    client.post("/login", data={"username": "apicust", "password": PASSWORD})

    response = client.get("/admin/menu", headers=API)
    assert response.status_code == 403
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["status"] == 403


def test_1f_browser_500_is_html_and_leaks_nothing(app, client, db):
    @app.get("/_kaboom")
    def _kaboom():
        raise RuntimeError("secret-internal-detail-12345")

    response = client.get("/_kaboom", headers=BROWSER)
    assert response.status_code == 500
    assert response.mimetype == "text/html"

    body = response.get_data(as_text=True)
    assert "Something went wrong" in body
    # Switching format must never widen disclosure.
    for leak in ("secret-internal-detail-12345", "Traceback", "RuntimeError",
                 "site-packages", "SECRET_KEY", "DATABASE_URL"):
        assert leak not in body, f"{leak!r} leaked into the HTML error page"


def test_1g_api_500_is_still_generic_json(app, client, db):
    @app.get("/_kaboom_json")
    def _kaboom_json():
        raise RuntimeError("another-internal-detail")

    response = client.get("/_kaboom_json", headers=API)
    assert response.status_code == 500
    assert response.get_json()["error"]["message"] == "An internal error occurred."
    assert "another-internal-detail" not in response.get_data(as_text=True)


# --- 2. Authentication redirect ----------------------------------------------


def test_2_unauthenticated_browser_is_redirected_to_login(client, db):
    response = client.get("/orders", headers=BROWSER)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_2b_next_carries_the_original_destination(client, db):
    response = client.get("/orders", headers=BROWSER)
    assert "next=%2Forders" in response.headers["Location"] \
        or "next=/orders" in response.headers["Location"]


def test_2c_unauthenticated_api_still_gets_401(client, db):
    """A 302 to an HTML form is useless to an API client."""
    response = client.get("/orders", headers=API)
    assert response.status_code == 401
    assert response.get_json()["error"]["status"] == 401


def test_2d_login_returns_the_user_to_next(client, db):
    make_user(username="nextuser", email="nextuser@example.com", password=PASSWORD)
    response = client.post("/login?next=/orders",
                           data={"username": "nextuser", "password": PASSWORD})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/orders")


@pytest.mark.parametrize("hostile", [
    "https://evil.example/phish",     # absolute URL
    "//evil.example/phish",           # protocol-relative
    "\\\\evil.example\\phish",        # backslashes some browsers normalise
    "http://evil.example",
])
def test_2e_next_cannot_be_used_as_an_open_redirect(client, db, hostile):
    """``next`` is attacker-controlled, so an unvalidated one would turn our
    own login page into a convincing phishing hop."""
    make_user(username="redirectuser", email="redirectuser@example.com", password=PASSWORD)
    response = client.post(f"/login?next={hostile}",
                           data={"username": "redirectuser", "password": PASSWORD})
    assert response.status_code == 302
    assert "evil.example" not in response.headers["Location"]


def test_2f_next_cannot_loop_back_to_login(client, db):
    make_user(username="looper", email="looper@example.com", password=PASSWORD)
    response = client.post("/login?next=/login",
                           data={"username": "looper", "password": PASSWORD})
    assert not response.headers["Location"].endswith("/login")


# --- 3. Currency formatting ---------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (Decimal("249.00"), "249.00"),      # already two places
    (Decimal("249"), "249.00"),         # integer-like Decimal
    (Decimal("249.5"), "249.50"),       # needs padding to two places
    (Decimal("0"), "0.00"),             # zero
    (Decimal("0.00"), "0.00"),
    (Decimal("1234.56"), "1234.56"),
    (Decimal("99999.99"), "99999.99"),  # top of the DECIMAL(10,2) demo range
])
def test_3_money_formats_decimals_exactly(value, expected):
    assert money(value) == f"{CURRENCY_SYMBOL}{expected}"


def test_3b_money_handles_missing_values():
    assert money(None) == MISSING
    assert money("") == MISSING
    assert money("not-a-number") == MISSING


def test_3c_money_never_routes_through_float():
    """0.1 and 0.7 are the classic values binary floating point cannot hold.

    If this ever went through ``float()`` the output would drift, and a price
    is the one number a customer will notice being a cent out.
    """
    assert money(Decimal("0.1")) == f"{CURRENCY_SYMBOL}0.10"
    assert money(Decimal("0.7")) == f"{CURRENCY_SYMBOL}0.70"
    assert money(Decimal("2.675")) == f"{CURRENCY_SYMBOL}2.68"  # quantized, not float-rounded


def test_3d_currency_appears_on_customer_pages(client, db):
    item = make_menu_item(category=make_category(name="Mains"),
                          name="Paneer Tikka", price="249.00")
    make_user(username="moneycust", email="moneycust@example.com", password=PASSWORD)
    client.post("/login", data={"username": "moneycust", "password": PASSWORD})

    assert CURRENCY_SYMBOL in client.get("/menu").get_data(as_text=True)
    assert CURRENCY_SYMBOL in client.get(f"/menu/{item.id}").get_data(as_text=True)

    client.post("/cart/add", data={"menu_item_id": item.id, "quantity": 2})
    cart = client.get("/cart").get_data(as_text=True)
    assert f"{CURRENCY_SYMBOL}249.00" in cart      # unit price
    assert f"{CURRENCY_SYMBOL}498.00" in cart      # line total and subtotal


def test_3e_currency_appears_on_order_pages(client, db):
    item = make_menu_item(category=make_category(name="Mains"),
                          name="Paneer Tikka", price="249.00")
    make_user(username="ordercust", email="ordercust@example.com", password=PASSWORD)
    client.post("/login", data={"username": "ordercust", "password": PASSWORD})
    client.post("/cart/add", data={"menu_item_id": item.id, "quantity": 2})
    client.post("/checkout", data={})

    history = client.get("/orders").get_data(as_text=True)
    assert f"{CURRENCY_SYMBOL}562.74" in history  # 498.00 + 13 % tax (M12)


def test_3f_stored_prices_are_unchanged_by_display(client, db):
    """Formatting is presentation only -- it must not touch the database."""
    item = make_menu_item(category=make_category(name="Mains"),
                          name="Paneer Tikka", price="249.00")
    client.get("/menu")
    from app.extensions import db as _db

    _db.session.expire_all()
    assert str(_db.session.get(type(item), item.id).price) == "249.00"


# --- 4. Registration signs the new account in --------------------------------


def _registration(**overrides) -> dict:
    data = {"username": "newcomer", "email": "newcomer@example.com",
            "password": PASSWORD, "password_confirm": PASSWORD}
    data.update(overrides)
    return data


def test_4_registration_creates_the_account(client, db):
    client.post("/register", data=_registration())
    user = db.session.query(User).filter_by(username="newcomer").first()
    assert user is not None
    assert user.role is Role.CUSTOMER          # role assignment unchanged
    assert user.password_hash != PASSWORD      # still hashed, never stored raw
    assert user.password_hash.startswith("$argon2")


def test_4b_registration_authenticates_the_new_user(client, db):
    client.post("/register", data=_registration())
    identity = client.get("/account/me")
    assert identity.status_code == 200
    assert identity.get_json()["username"] == "newcomer"


def test_4c_registration_redirects_to_preferences(client, db):
    response = client.post("/register", data=_registration(), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/preferences")


def test_4d_duplicate_registration_still_fails(app, client, db):
    from flask import g

    client.post("/register", data=_registration())
    g.pop("current_user", None)                 # the first account is now signed in
    other = app.test_client()

    response = other.post("/register", data=_registration(email="different@example.com"))
    assert response.status_code == 200          # re-rendered form, not a redirect
    assert b"already taken" in response.data
    assert db.session.query(User).filter_by(email="different@example.com").first() is None


@pytest.mark.parametrize("bad,field", [
    ({"password": "short", "password_confirm": "short"}, b"at least"),
    ({"password_confirm": "does-not-match"}, b"match"),
    ({"email": "not-an-email"}, b"email"),
    ({"username": ""}, b"required"),
])
def test_4e_invalid_registration_still_fails(client, db, bad, field):
    response = client.post("/register", data=_registration(**bad))
    assert response.status_code == 200
    assert field.lower() in response.data.lower()
    assert db.session.query(User).filter_by(username="newcomer").first() is None


def test_4f_registration_still_requires_csrf(app, client, db):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post("/register", data=_registration())
        assert response.status_code == 400
        assert db.session.query(User).filter_by(username="newcomer").first() is None
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


# --- 5. Model warm-up ---------------------------------------------------------


def test_5_warmup_is_inert_under_testing(app):
    """Tests must never load a 1.2 GB model as a side effect of create_app."""
    from app.services import local_llm

    assert app.config["TESTING"] is True
    assert local_llm.warm_up(app) is False


def test_5b_warmup_respects_the_configuration_switches(app, monkeypatch):
    from app.services import local_llm

    monkeypatch.setattr(local_llm, "_warmup_started", False)
    # Disabled model: nothing to warm.
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "LLM_ENABLED", False)
    monkeypatch.setitem(app.config, "LLM_WARMUP", True)
    assert local_llm.warm_up(app) is False

    # Enabled model but warm-up switched off.
    monkeypatch.setitem(app.config, "LLM_ENABLED", True)
    monkeypatch.setitem(app.config, "LLM_WARMUP", False)
    assert local_llm.warm_up(app) is False


def test_5c_warmup_starts_at_most_one_thread(app, monkeypatch):
    """Two concurrent loads would mean two resident copies of the model."""
    from app.services import local_llm

    started = []
    monkeypatch.setattr(local_llm, "_warmup_started", False)
    monkeypatch.setattr(local_llm.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: started.append(1)})())
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "LLM_ENABLED", True)
    monkeypatch.setitem(app.config, "LLM_WARMUP", True)
    monkeypatch.setitem(app.config, "DEBUG", False)

    assert local_llm.warm_up(app) is True
    assert local_llm.warm_up(app) is False       # idempotent
    assert len(started) == 1


def test_5d_production_does_not_warm_up_by_default():
    """N workers would mean N resident models, so production must opt in."""
    from config import DevelopmentConfig, ProductionConfig

    assert ProductionConfig.LLM_WARMUP is False
    assert DevelopmentConfig.LLM_WARMUP is True


def test_5e_production_adapter_is_still_v4():
    """The polish phase must not have moved production off V4."""
    from pathlib import Path

    from config import BaseConfig

    assert Path(BaseConfig.LLM_ADAPTER_PATH).name == "qwen3-0.6b-quickjunction-lora-v4"
