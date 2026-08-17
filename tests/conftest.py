"""Shared pytest fixtures.

The testing configuration points at an in-memory SQLite database, so the
suite runs with no MySQL server and touches no real data.
"""

from __future__ import annotations

import re

import pytest
from flask import Flask
from flask.testing import FlaskClient

from decimal import Decimal

from app import create_app
from app.extensions import db as _db
from app.models.category import Category
from app.models.enums import Cuisine, DietaryType, SpiceLevel
from app.models.menu_item import MenuItem
from app.models.user import Role, User
from app.utils.security import hash_password

_CSRF_TAG_RE = re.compile(r'<input[^>]*name="csrf_token"[^>]*>')
_VALUE_RE = re.compile(r'value="([^"]+)"')


@pytest.fixture
def app() -> Flask:
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def db(app: Flask):
    return _db


def make_user(username: str = "alice", email: str = "alice@example.com", password: str = "correct-horse-1",
              role: Role = Role.CUSTOMER, is_active: bool = True) -> User:
    """Insert a user directly, bypassing HTTP -- for tests that need an
    account to already exist (login, authorization checks)."""
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role=role,
        is_active=is_active,
    )
    _db.session.add(user)
    _db.session.commit()
    return user


def make_category(name: str = "Starters", is_active: bool = True) -> Category:
    category = Category(name=name, description=None, is_active=is_active)
    _db.session.add(category)
    _db.session.commit()
    return category


def make_menu_item(
    category: Category | None = None,
    name: str = "Paneer Tikka",
    price: str = "199.00",
    cuisine: Cuisine = Cuisine.INDIAN,
    spice_level: SpiceLevel = SpiceLevel.MEDIUM,
    dietary_type: DietaryType = DietaryType.VEGETARIAN,
    is_available: bool = True,
) -> MenuItem:
    """Insert a menu item directly, bypassing HTTP/the service layer -- for
    tests that need one to already exist."""
    if category is None:
        category = make_category()
    item = MenuItem(
        category_id=category.id,
        name=name,
        description=None,
        price=Decimal(price),
        cuisine=cuisine,
        spice_level=spice_level,
        dietary_type=dietary_type,
        is_available=is_available,
    )
    _db.session.add(item)
    _db.session.commit()
    return item


def csrf_token(client: FlaskClient, get_url: str) -> str:
    """Fetch a form page and pull its CSRF token, the way a browser would
    submit it back."""
    response = client.get(get_url)
    tag_match = _CSRF_TAG_RE.search(response.get_data(as_text=True))
    assert tag_match, f"no CSRF field found on {get_url}"
    value_match = _VALUE_RE.search(tag_match.group(0))
    assert value_match, f"CSRF field on {get_url} has no value"
    return value_match.group(1)
