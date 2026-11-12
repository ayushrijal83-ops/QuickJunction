"""S1-A: the Redis sliding window, against a real Redis server.

Opt-in, like the MySQL-only tests: set ``TEST_REDIS_URL=redis://host:port/db``.
CI's ``test-redis`` job does, and fails if any test here is skipped.

Safe against a shared server: every test writes only under its own random
key prefix and deletes exactly those keys afterwards. Nothing here flushes a
database.

Each ``worker()`` is its own ``SharedBackend`` -- its own client and
connection pool -- standing in for a separate gunicorn worker process.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time

import pytest
from flask import g

import app.utils.ratelimit as ratelimit
from app import create_app
from app.extensions import db as _db
from app.models.user import User
from app.services.tables import create_table
from app.utils.ratelimit import FailureRateLimiter, RequestRateLimiter, SharedBackend, login_limiter
from config import TestingConfig
from tests.conftest import make_user

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_REDIS_URL, reason="Redis rate-limiter tests need TEST_REDIS_URL=redis://host:port/db")

SECRET = "testing-only-not-a-secret"


@pytest.fixture
def prefix() -> str:
    return f"qjtest:{secrets.token_hex(8)}"


@pytest.fixture
def raw(prefix):
    """A plain client for looking at what the limiter stored, and for cleanup."""
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    client.ping()
    yield client
    for key in client.scan_iter(match=f"{prefix}:*"):
        client.delete(key)
    client.close()


@pytest.fixture
def worker(prefix, raw):
    backends: list[SharedBackend] = []

    def make() -> SharedBackend:
        backend = SharedBackend()
        backend.configure(TEST_REDIS_URL, secret=SECRET, prefix=prefix, timeout=1.0)
        backends.append(backend)
        return backend

    yield make
    for backend in backends:
        backend.configure(None, secret=SECRET)


def keys(raw, prefix) -> list[str]:
    return sorted(raw.scan_iter(match=f"{prefix}:*"))


def run_together(actions) -> list:
    start = threading.Barrier(len(actions))
    results: list = []

    def run(action) -> None:
        start.wait()
        results.append(action())

    threads = [threading.Thread(target=run, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


# --- limits and boundaries -------------------------------------------------------


def test_allows_exactly_the_limit_then_refuses(worker):
    limiter = RequestRateLimiter("checkout", 3, 60, backend=worker())
    decisions = [limiter.attempt("user-1") for _ in range(5)]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert [d.count for d in decisions] == [1, 2, 3, 3, 3]
    assert all(d.shared and d.limit == 3 for d in decisions)


def test_identifiers_and_policies_do_not_share_a_counter(worker, raw, prefix):
    backend = worker()
    checkout = RequestRateLimiter("checkout", 1, 60, backend=backend)
    reservation = RequestRateLimiter("reservation", 1, 60, backend=backend)
    assert checkout.attempt("user-1").allowed
    assert checkout.attempt("user-2").allowed
    assert reservation.attempt("user-1").allowed
    assert not checkout.attempt("user-1").allowed
    assert len(keys(raw, prefix)) == 3


def test_a_refused_request_is_not_recorded(worker, raw, prefix):
    limiter = RequestRateLimiter("checkout", 2, 60, backend=worker())
    for _ in range(12):
        limiter.attempt("u")
    (key,) = keys(raw, prefix)
    assert raw.zcard(key) == 2


def test_window_slides_and_entries_expire(worker):
    limiter = RequestRateLimiter("checkout", 2, 1.0, backend=worker())
    assert limiter.attempt("u").allowed  # t = 0
    time.sleep(0.5)
    assert limiter.attempt("u").allowed  # t = 0.5
    assert not limiter.attempt("u").allowed  # both inside the window
    time.sleep(0.7)  # t = 1.2: the first entry is older than the window, the second is not
    decision = limiter.attempt("u")
    assert decision.allowed and decision.count == 2
    assert not limiter.attempt("u").allowed
    time.sleep(1.2)  # everything is older than the window
    assert limiter.attempt("u").count == 1


def test_key_expires_with_the_window(worker, raw, prefix):
    limiter = RequestRateLimiter("checkout", 5, 30, backend=worker())
    limiter.attempt("u")
    (key,) = keys(raw, prefix)
    assert 0 < raw.pttl(key) <= 30_000
    short = RequestRateLimiter("ai_explain", 5, 0.3, backend=worker())
    short.attempt("u")
    time.sleep(0.6)
    assert keys(raw, prefix) == [key]  # the short-window key is gone on its own


# --- unique members and atomicity ------------------------------------------------


def test_every_request_is_its_own_member_even_when_simultaneous(worker, raw, prefix):
    """40 requests released together through 8 separate clients: each is one
    entry. If a member were derived from the timestamp, requests sharing a
    timestamp would overwrite each other and the count would come up short."""
    limiters = [RequestRateLimiter("checkout", 1000, 60, backend=worker()) for _ in range(8)]
    results = run_together([(lambda limiter=limiters[i % 8]: limiter.attempt("u")) for i in range(40)])
    assert all(d.allowed for d in results)
    assert sorted(d.count for d in results) == list(range(1, 41))  # each saw a distinct count
    (key,) = keys(raw, prefix)
    members = raw.zrange(key, 0, -1)
    assert len(members) == len(set(members)) == 40
    assert all(re.fullmatch(r"[0-9a-f]{32}", member) for member in members)


def test_concurrent_attempts_across_clients_never_exceed_the_limit(worker, raw, prefix):
    for round_number in range(5):
        identifier = f"user-{round_number}"
        limiters = [RequestRateLimiter("checkout", 10, 60, backend=worker()) for _ in range(8)]
        results = run_together([(lambda limiter=limiters[i % 8]: limiter.attempt(identifier)) for i in range(40)])
        assert [d.allowed for d in results].count(True) == 10, f"round {round_number}"
    assert all(raw.zcard(key) == 10 for key in keys(raw, prefix))


# --- independent clients ---------------------------------------------------------


def test_independent_clients_observe_the_same_counter(worker):
    worker_a = RequestRateLimiter("checkout", 3, 60, backend=worker())
    worker_b = RequestRateLimiter("checkout", 3, 60, backend=worker())
    assert worker_a.attempt("u").count == 1
    assert worker_b.attempt("u").count == 2
    assert worker_a.attempt("u").count == 3
    assert not worker_b.attempt("u").allowed
    worker_b.reset("u")
    assert worker_a.attempt("u").count == 1


def test_login_failures_are_shared_across_workers(worker):
    worker_a = FailureRateLimiter("login", 5, 900, backend=worker())
    worker_b = FailureRateLimiter("login", 5, 900, backend=worker())
    key = "203.0.113.9:alice"
    for _ in range(3):
        worker_a.record_failure(key)
    assert not worker_b.is_limited(key)
    for _ in range(2):
        worker_b.record_failure(key)
    assert worker_a.is_limited(key) and worker_b.is_limited(key)
    assert not worker_a.is_limited("203.0.113.9:bob")  # same IP, another account
    assert not worker_a.is_limited("198.51.100.7:alice")  # same account, another IP
    worker_a.reset(key)  # a successful login on one worker...
    assert not worker_b.is_limited(key)  # ...clears the limit on the other


def test_failures_expire_after_the_window(worker):
    limiter = FailureRateLimiter("login", 2, 0.5, backend=worker())
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.is_limited("k")
    time.sleep(0.8)
    assert not limiter.is_limited("k")


def test_keys_are_hmac_digests_not_identifiers(worker, raw, prefix):
    limiter = FailureRateLimiter("login", 5, 900, backend=worker())
    limiter.record_failure("203.0.113.9:alice")
    (key,) = keys(raw, prefix)
    assert re.fullmatch(re.escape(prefix) + r":login:[0-9a-f]{64}", key)
    assert "alice" not in key and "203.0.113.9" not in key


# --- the atomic login proposal -----------------------------------------------------


def test_two_step_check_overshoots_under_concurrency_and_try_reserve_does_not(worker):
    """Same race, both ways, across separate clients. Two-step: every
    request that checks before the fifth failure is recorded goes on to
    record one. Reserved: the limit holds exactly."""
    two_step = [FailureRateLimiter("login", 5, 900, backend=worker()) for _ in range(4)]
    key = "203.0.113.9:alice"
    checked = [limiter.is_limited(key) for limiter in two_step for _ in range(3)]  # 12 requests pass the check
    assert checked == [False] * 12
    for limiter in two_step:
        for _ in range(3):
            limiter.record_failure(key)
    assert two_step[0]._backend._store.count("login", key, 900) == 12  # 12 guesses against a limit of 5

    reserved = [FailureRateLimiter("login", 5, 900, backend=worker()) for _ in range(4)]
    other = "203.0.113.9:bob"
    results = run_together([(lambda limiter=reserved[i % 4]: limiter.try_reserve(other)) for i in range(24)])
    assert [d.allowed for d in results].count(True) == 5
    assert reserved[0]._backend._store.count("login", other, 900) == 5


# --- outage --------------------------------------------------------------------------


def test_losing_redis_mid_flight_falls_back_and_recovers(worker, monkeypatch):
    import redis

    backend = worker()
    limiter = RequestRateLimiter("checkout", 2, 60, backend=backend)
    assert limiter.attempt("u").shared

    healthy_hit = backend._store._hit

    def unreachable(*args, **kwargs):
        raise redis.ConnectionError("connection lost")

    monkeypatch.setattr(backend._store, "_hit", unreachable)
    decisions = [limiter.attempt("u") for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]  # limited locally
    assert not any(d.shared for d in decisions)
    assert backend.status()["degraded"] is True

    monkeypatch.setattr(backend._store, "_hit", healthy_hit)
    monkeypatch.setattr(ratelimit, "_now", lambda: time.monotonic() + SharedBackend.RETRY_SECONDS)
    decision = limiter.attempt("u")
    assert decision.shared and decision.count == 2  # Redis still holds the one request from before
    assert backend.status()["degraded"] is False


# --- the application, wired to Redis ---------------------------------------------------


@pytest.fixture
def redis_app(monkeypatch, prefix, raw):
    monkeypatch.setattr(TestingConfig, "REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setattr(TestingConfig, "RATELIMIT_KEY_PREFIX", prefix)
    monkeypatch.setattr(TestingConfig, "SQLALCHEMY_DATABASE_URI", "sqlite://")
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()
    ratelimit._default_backend.configure(None, secret=SECRET)


def test_login_route_is_limited_through_redis(redis_app, worker, raw, prefix):
    login_limiter._attempts.clear()
    make_user(username="alice", password="correct-horse-1")
    client = redis_app.test_client()

    def login(password: str):
        return client.post("/login", data={"username": "alice", "password": password})

    assert ratelimit.rate_limit_status()["shared"] is True
    for _ in range(5):
        assert login("wrong-password-1").status_code != 429
    assert login("correct-horse-1").status_code == 429  # the sixth is refused, as before
    assert login_limiter._attempts == {}  # nothing was counted in this process

    (key,) = keys(raw, prefix)
    assert raw.zcard(key) == 5
    another_worker = FailureRateLimiter("login", 5, 900, backend=worker())
    assert another_worker.is_limited("127.0.0.1:alice")  # a second worker enforces the same limit
    another_worker.reset("127.0.0.1:alice")
    assert login("correct-horse-1").status_code == 302


# --- S1-B: reservations, Retry-After, release ------------------------------------


def test_refusal_reports_when_the_oldest_entry_leaves_the_window(worker):
    limiter = RequestRateLimiter("checkout", 2, 60, backend=worker())
    limiter.attempt("u")
    limiter.attempt("u")
    refused = limiter.attempt("u")
    assert not refused.allowed and refused.shared
    assert 59 < refused.retry_after <= 60  # from the server's clock, in seconds


def test_release_removes_exactly_one_reservation(worker, raw, prefix):
    limiter = FailureRateLimiter("login", 5, 900, backend=worker())
    first, second, third = (limiter.try_reserve("k") for _ in range(3))
    assert all(d.allowed and d.shared and d.token for d in (first, second, third))
    limiter.release("k", second.token)
    (key,) = keys(raw, prefix)
    assert sorted(raw.zrange(key, 0, -1)) == sorted([first.token, third.token])
    limiter.release("k", "not-a-token")
    assert raw.zcard(key) == 2


def test_an_unresolved_reservation_expires_with_the_window(worker, raw, prefix):
    """A worker that dies between reserving and resolving: its entry ages
    out like any failure and the key expires, so nobody stays locked out."""
    limiter = FailureRateLimiter("login", 2, 1, backend=worker())
    assert limiter.try_reserve("k").allowed and limiter.try_reserve("k").allowed  # never resolved
    assert not limiter.try_reserve("k").allowed
    (key,) = keys(raw, prefix)
    assert 0 < raw.pttl(key) <= 1000
    time.sleep(1.1)
    assert limiter.try_reserve("k").allowed


def test_concurrent_reservations_across_clients_admit_only_the_free_slots(worker):
    limiters = [FailureRateLimiter("login", 5, 900, backend=worker()) for _ in range(4)]
    for _ in range(4):
        limiters[0].record_failure("k")
    results = run_together([(lambda limiter=limiters[i % 4]: limiter.try_reserve("k")) for i in range(20)])
    winners = [d for d in results if d.allowed]
    assert len(winners) == 1  # one free slot, one attempt in flight
    limiters[1].release("k", winners[0].token)  # any worker can withdraw it
    assert limiters[2]._backend._store.count("login", "k", 900) == 4


# --- S1-B: the routes, wired to Redis ---------------------------------------------


def digest(identifier: str) -> str:
    return hmac.new(SECRET.encode(), identifier.encode(), hashlib.sha256).hexdigest()


def req(client, method, url, **kw):
    g.pop("current_user", None)  # known issue #29: one app context, shared g
    return getattr(client, method)(url, **kw)


def signed_in(application, username):
    make_user(username=username, email=f"{username}@example.com", password="correct-horse-1")
    client = application.test_client()
    response = req(client, "post", "/login", data={"username": username, "password": "correct-horse-1"})
    assert response.status_code == 302
    return client


def test_customer_routes_count_in_redis_under_hmac_keys(redis_app, worker, raw, prefix, monkeypatch):
    monkeypatch.setattr("app.routes.preferences.generate_explanation", lambda r, **kw: "fine")
    client = signed_in(redis_app, "alice")
    alice_id = str(_db.session.query(User).filter_by(username="alice").one().id)
    create_table("T01", 4)

    for _ in range(8):
        assert req(client, "post", "/checkout", data={}).status_code == 302
    for _ in range(10):
        assert req(client, "post", "/reservations", data={}).status_code == 302
    for _ in range(4):
        assert req(client, "get", "/recommendations/explain").status_code == 200
    for refused in (req(client, "post", "/checkout", data={}), req(client, "post", "/reservations", data={}),
                    req(client, "get", "/recommendations/explain")):
        assert refused.status_code == 429 and int(refused.headers["Retry-After"]) >= 1

    expected = {f"{prefix}:{policy}:{digest(alice_id)}": count
                for policy, count in (("checkout", 8), ("reservation", 10), ("ai_explanation", 4))}
    assert {key: raw.zcard(key) for key in keys(raw, prefix)} == expected  # the login reset its own key
    assert not RequestRateLimiter("checkout", 8, 300, backend=worker()).attempt(alice_id).allowed
    assert ratelimit.checkout_limiter._local._attempts == {}  # nothing counted in this process


def test_ip_routes_count_in_redis_and_ignore_rotating_identities(redis_app, raw, prefix):
    client = redis_app.test_client()
    for n in range(10):
        # Invalid sign-ups under rotating names, and wrong codes: all counted per IP.
        req(client, "post", "/register", data={"username": f"x{n}", "email": f"bad{n}", "password": "a"})
        req(client, "post", "/account/setup", data={"code": f"guess-{n}", "password": "a", "password_confirm": "a"})
    assert req(client, "post", "/register", data={"username": "fresh"}).status_code == 429
    assert req(client, "post", "/account/setup", data={"code": "another"}).status_code == 429
    assert set(keys(raw, prefix)) == {f"{prefix}:registration:{digest('127.0.0.1')}",
                                      f"{prefix}:account_setup:{digest('127.0.0.1')}"}
    assert all("127.0.0.1" not in key for key in keys(raw, prefix))


def test_password_change_counts_only_wrong_current_passwords_in_redis(redis_app, raw, prefix):
    client = signed_in(redis_app, "alice")
    alice_id = _db.session.query(User).filter_by(username="alice").one().id

    def change(current, new="brand-new-pass-42"):
        return req(client, "post", "/account/password",
                   data={"current_password": current, "password": new, "password_confirm": new})

    for _ in range(4):
        assert change("wrong-current-1").status_code == 200
    for _ in range(3):
        assert change("correct-horse-1", new="short").status_code == 200  # a policy error: released
    key = f"{prefix}:password_change:{digest(f'password-change:{alice_id}')}"
    assert raw.zcard(key) == 4
    assert change("wrong-current-1").status_code == 200
    assert change("correct-horse-1").status_code == 429


def test_login_attempt_in_flight_is_visible_to_other_workers(redis_app, worker, monkeypatch):
    import app.routes.auth as auth_routes

    make_user(username="alice", password="correct-horse-1")
    client = redis_app.test_client()
    for _ in range(4):
        req(client, "post", "/login", data={"username": "alice", "password": "wrong-password-1"})
    another_worker = FailureRateLimiter("login", 5, 900, backend=worker())
    real, seen = auth_routes.authenticate_user, {}

    def check(username, password):
        seen["other worker"] = another_worker.try_reserve("127.0.0.1:alice").allowed
        return real(username, password)

    monkeypatch.setattr(auth_routes, "authenticate_user", check)
    response = req(client, "post", "/login", data={"username": "alice", "password": "correct-horse-1"})
    assert response.status_code == 302
    assert seen["other worker"] is False  # the fifth slot was held while the password was checked
    assert another_worker._backend._store.count("login", "127.0.0.1:alice", 900) == 0  # success reset it


def test_route_outage_falls_back_locally_and_recovers(redis_app, monkeypatch):
    import redis

    client = signed_in(redis_app, "alice")
    for _ in range(3):
        req(client, "post", "/checkout", data={})
    store = ratelimit._default_backend._store
    healthy = store._hit

    def unreachable(*args, **kwargs):
        raise redis.ConnectionError("connection lost")

    monkeypatch.setattr(store, "_hit", unreachable)
    statuses = [req(client, "post", "/checkout", data={}).status_code for _ in range(9)]
    assert statuses == [302] * 8 + [429]  # still limited, by this process alone
    assert ratelimit.rate_limit_status()["degraded"] is True

    monkeypatch.setattr(store, "_hit", healthy)
    monkeypatch.setattr(ratelimit, "_now", lambda: time.monotonic() + SharedBackend.RETRY_SECONDS)
    statuses = [req(client, "post", "/checkout", data={}).status_code for _ in range(6)]
    assert statuses == [302] * 5 + [429]  # Redis still held the 3 from before the outage
    assert ratelimit.rate_limit_status()["shared"] is True
