"""S1-A: the rate-limiter foundation, without a Redis server.

Covers the in-memory window, the two limiter APIs, the existing login
limiter's behaviour, the fallback and its warnings (driven by a stand-in
store that can be switched off), and REDIS_URL configuration. The Lua
scripts themselves only run against a real Redis: tests/test_ratelimit_redis.py.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import app.utils.ratelimit as ratelimit
from app.utils.logging import get_security_logger
from app.utils.ratelimit import (
    FailureRateLimiter,
    RequestRateLimiter,
    SharedBackend,
    _MemoryWindow,
    login_limiter,
    password_change_limiter,
    rate_limit_status,
)
from config import BaseConfig, ConfigError, ProductionConfig, TestingConfig, redis_url_is_supported

BASE_DIR = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(ratelimit, "_now", clock)
    return clock


@pytest.fixture
def security_log(monkeypatch):
    """Records what the limiter writes to the security logger. That logger
    does not propagate (so caplog misses it), and Alembic's fileConfig, run by
    any earlier migration test, may have disabled it."""
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = get_security_logger()
    handler = Collect(level=logging.DEBUG)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


class StandInStore:
    """Behaves like the Redis store (same four operations) but counts in
    memory, and raises ConnectionError while ``down``."""

    errors = (ConnectionError,)

    def __init__(self) -> None:
        self.down = False
        self.calls = 0
        self._windows: dict[tuple[str, int, float], _MemoryWindow] = {}

    def _window(self, policy: str, limit: int, window_seconds: float) -> _MemoryWindow:
        return self._windows.setdefault((policy, limit, window_seconds), _MemoryWindow(limit, window_seconds))

    def _call(self) -> None:
        self.calls += 1
        if self.down:
            raise ConnectionError("redis://:do-not-log-me@10.0.0.9:6379 refused")

    def hit(self, policy, identifier, limit, window_seconds):
        self._call()
        return self._window(policy, limit, window_seconds).hit(identifier)

    def add(self, policy, identifier, window_seconds):
        self._call()
        return self._window(policy, 0, window_seconds).add(identifier)

    def count(self, policy, identifier, window_seconds):
        self._call()
        return self._window(policy, 0, window_seconds).count(identifier)

    def reset(self, policy, identifier):
        self._call()
        for (name, _, _), window in self._windows.items():
            if name == policy:
                window.reset(identifier)

    def close(self) -> None:
        pass


def backend_with(store: StandInStore) -> SharedBackend:
    backend = SharedBackend()
    backend._store = store
    return backend


# --- in-memory window ---------------------------------------------------------


def test_request_limiter_allows_exactly_the_limit(clock):
    limiter = RequestRateLimiter("checkout", 3, 60, backend=None)
    decisions = [limiter.attempt("user-1") for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.count for d in decisions] == [1, 2, 3, 3]
    assert all(d.limit == 3 and d.shared is False for d in decisions)


def test_identifiers_are_counted_separately(clock):
    limiter = RequestRateLimiter("checkout", 1, 60, backend=None)
    assert limiter.attempt("user-1").allowed
    assert limiter.attempt("user-2").allowed
    assert not limiter.attempt("user-1").allowed


def test_a_refused_request_is_not_recorded(clock):
    limiter = RequestRateLimiter("checkout", 2, 60, backend=None)
    limiter.attempt("u")
    limiter.attempt("u")
    for _ in range(10):
        assert not limiter.attempt("u").allowed
    clock.advance(60)  # the two recorded requests expire; the ten refusals left nothing behind
    assert limiter.attempt("u").count == 1


def test_window_slides_and_expires_at_the_boundary(clock):
    limiter = RequestRateLimiter("checkout", 2, 60, backend=None)
    assert limiter.attempt("u").allowed  # t = 0
    clock.advance(20)
    assert limiter.attempt("u").allowed  # t = 20
    clock.advance(39.9)
    assert not limiter.attempt("u").allowed  # t = 59.9: both still inside
    clock.advance(0.1)
    assert limiter.attempt("u").allowed  # t = 60: the first is exactly one window old, so gone
    assert not limiter.attempt("u").allowed  # t = 20 and t = 60 remain
    clock.advance(20)
    assert limiter.attempt("u").allowed  # t = 80: the t = 20 entry is gone


def test_concurrent_attempts_in_one_process_never_exceed_the_limit():
    limiter = RequestRateLimiter("checkout", 10, 60, backend=None)
    start = threading.Barrier(40)
    allowed: list[bool] = []

    def worker() -> None:
        start.wait()
        allowed.append(limiter.attempt("u").allowed)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert allowed.count(True) == 10


@pytest.mark.parametrize("name", ["Login", "log in", "login:x", "", "1login"])
def test_policy_names_are_restricted(name):
    with pytest.raises(ValueError):
        RequestRateLimiter(name, 1, 1, backend=None)


@pytest.mark.parametrize("limit,window", [(0, 60), (5, 0), (-1, 60)])
def test_limit_and_window_must_be_positive(limit, window):
    with pytest.raises(ValueError):
        FailureRateLimiter("login", limit, window, backend=None)


# --- failure limiter and the existing login limiter ------------------------------


def test_failure_limiter_keeps_the_three_method_interface(clock):
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    key = "203.0.113.9:alice"
    for _ in range(4):
        limiter.record_failure(key)
    assert not limiter.is_limited(key)
    limiter.record_failure(key)
    assert limiter.is_limited(key)
    limiter.reset(key)
    assert not limiter.is_limited(key)


def test_failures_expire_after_the_window(clock):
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    for _ in range(5):
        limiter.record_failure("k")
    clock.advance(899)
    assert limiter.is_limited("k")
    clock.advance(1)
    assert not limiter.is_limited("k")


def test_login_limiter_policy_is_unchanged():
    assert isinstance(login_limiter, FailureRateLimiter)
    assert (login_limiter.name, login_limiter.limit, login_limiter.window_seconds) == ("login", 5, 900)
    assert login_limiter._backend is ratelimit._default_backend


def test_login_limiter_attempts_dict_is_still_what_tests_clear():
    """Five existing test modules reset the limiter with
    ``login_limiter._attempts.clear()``."""
    login_limiter._attempts.clear()
    try:
        for _ in range(5):
            login_limiter.record_failure("198.51.100.1:bob")
        assert "198.51.100.1:bob" in login_limiter._attempts
        assert login_limiter.is_limited("198.51.100.1:bob")
        login_limiter._attempts.clear()
        assert not login_limiter.is_limited("198.51.100.1:bob")
    finally:
        login_limiter._attempts.clear()


def test_password_change_limiter_stays_in_memory_in_the_same_store():
    assert (password_change_limiter.limit, password_change_limiter.window_seconds) == (5, 900)
    assert password_change_limiter._backend is None
    assert password_change_limiter._attempts is login_limiter._attempts


def test_password_change_limiter_ignores_a_configured_shared_store(monkeypatch):
    store = StandInStore()
    monkeypatch.setattr(ratelimit._default_backend, "_store", store)
    login_limiter._attempts.clear()
    try:
        password_change_limiter.record_failure("password-change:7")
        assert store.calls == 0
        assert "password-change:7" in login_limiter._attempts
        login_limiter.record_failure("198.51.100.1:bob")
        assert store.calls == 1
        assert "198.51.100.1:bob" not in login_limiter._attempts
    finally:
        login_limiter._attempts.clear()


# --- the login check-then-record boundary ---------------------------------------


def test_two_step_login_check_lets_concurrent_attempts_overshoot(clock):
    """Documents today's behaviour, unchanged by S1-A: ``is_limited`` and
    ``record_failure`` are separate steps with the password check between
    them, so attempts already past the check are not stopped by the limit."""
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    key = "203.0.113.9:alice"
    for _ in range(4):
        limiter.record_failure(key)
    in_flight = [limiter.is_limited(key) for _ in range(3)]  # three requests check before any records
    assert in_flight == [False, False, False]
    for _ in in_flight:
        limiter.record_failure(key)  # all three passwords were wrong
    assert len(limiter._attempts[key]) == 7  # two more guesses than the limit of 5


def test_try_reserve_closes_that_gap(clock):
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    key = "203.0.113.9:alice"
    for _ in range(4):
        limiter.record_failure(key)
    in_flight = [limiter.try_reserve(key).allowed for _ in range(3)]
    assert in_flight == [True, False, False]  # one more guess, then the limit holds
    assert len(limiter._attempts[key]) == 5
    assert limiter.is_limited(key)


def test_try_reserve_then_reset_on_success_matches_login_semantics(clock):
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    key = "203.0.113.9:alice"
    for _ in range(4):
        assert limiter.try_reserve(key).allowed  # four wrong passwords: nothing to undo
    assert limiter.try_reserve(key).allowed  # the fifth attempt is still let through...
    limiter.reset(key)  # ...and it was correct
    assert not limiter.is_limited(key)
    for _ in range(5):
        assert limiter.try_reserve(key).allowed
    assert not limiter.try_reserve(key).allowed  # the sixth attempt in a window is refused unchecked


def test_try_reserve_is_atomic_across_threads():
    limiter = FailureRateLimiter("login", 5, 900, backend=None)
    start = threading.Barrier(30)
    allowed: list[bool] = []

    def worker() -> None:
        start.wait()
        allowed.append(limiter.try_reserve("k").allowed)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert allowed.count(True) == 5


# --- shared store, outage and fallback ------------------------------------------


def test_shared_store_is_used_when_it_answers(clock):
    store = StandInStore()
    limiter = RequestRateLimiter("checkout", 2, 60, backend=backend_with(store))
    decisions = [limiter.attempt("u") for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert all(d.shared for d in decisions)
    assert limiter._local._attempts == {}  # nothing counted locally while the store works


def test_two_workers_share_one_store(clock):
    store = StandInStore()
    worker_a = FailureRateLimiter("login", 5, 900, backend=backend_with(store))
    worker_b = FailureRateLimiter("login", 5, 900, backend=backend_with(store))
    for _ in range(3):
        worker_a.record_failure("k")
    for _ in range(2):
        worker_b.record_failure("k")
    assert worker_a.is_limited("k") and worker_b.is_limited("k")
    worker_b.reset("k")
    assert not worker_a.is_limited("k")


def test_outage_falls_back_to_in_memory_limits(clock, security_log):
    store = StandInStore()
    backend = backend_with(store)
    limiter = RequestRateLimiter("checkout", 2, 60, backend=backend)
    store.down = True
    decisions = [limiter.attempt("u") for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]  # still limited
    assert not any(d.shared for d in decisions)  # and never reported as shared
    status = backend.status()
    assert status == {"backend": "redis", "shared": False, "degraded": True,
                      "degraded_since": status["degraded_since"], "last_error": "ConnectionError"}
    assert status["degraded_since"] is not None


def test_outage_does_not_retry_the_store_on_every_request(clock):
    store = StandInStore()
    limiter = RequestRateLimiter("checkout", 100, 60, backend=backend_with(store))
    store.down = True
    for _ in range(20):
        limiter.attempt("u")
    assert store.calls == 1
    clock.advance(SharedBackend.RETRY_SECONDS)
    limiter.attempt("u")
    assert store.calls == 2


def test_outage_warning_is_throttled_per_policy(clock, security_log):
    store = StandInStore()
    backend = backend_with(store)
    checkout = RequestRateLimiter("checkout", 100, 60, backend=backend)
    login = FailureRateLimiter("login", 5, 900, backend=backend)
    store.down = True

    def warnings() -> list[str]:
        return [r.getMessage() for r in security_log if r.levelno == logging.WARNING]

    for _ in range(50):
        checkout.attempt("u")
    assert len(warnings()) == 1 and "policy=checkout" in warnings()[0]
    login.is_limited("k")
    assert len(warnings()) == 2 and "policy=login" in warnings()[1]  # each policy warns for itself
    clock.advance(59)
    checkout.attempt("u")
    assert len(warnings()) == 2  # under a minute: still quiet
    clock.advance(1)
    checkout.attempt("u")
    assert len(warnings()) == 3
    assert all("NOT shared across workers" in message for message in warnings())


def test_outage_warning_never_contains_the_url_or_the_error_text(clock, security_log):
    store = StandInStore()
    limiter = RequestRateLimiter("checkout", 5, 60, backend=backend_with(store))
    store.down = True
    limiter.attempt("203.0.113.9:alice")
    text = " ".join(r.getMessage() for r in security_log)
    assert "ConnectionError" in text
    for leak in ("do-not-log-me", "10.0.0.9", "redis://", "alice", "203.0.113.9"):
        assert leak not in text


def test_recovery_returns_to_the_shared_store_and_is_logged(clock, security_log):
    store = StandInStore()
    backend = backend_with(store)
    limiter = RequestRateLimiter("checkout", 2, 60, backend=backend)
    assert limiter.attempt("u").shared
    store.down = True
    assert not limiter.attempt("u").shared
    store.down = False
    assert not limiter.attempt("u").shared  # still inside the retry interval
    clock.advance(SharedBackend.RETRY_SECONDS)
    decision = limiter.attempt("u")
    assert decision.shared and decision.count == 2  # the store's own count: 1 before the outage + this one
    assert backend.status()["degraded"] is False and backend.status()["shared"] is True
    assert any(r.levelno == logging.INFO and "restored" in r.getMessage() for r in security_log)
    store.down = True
    clock.advance(SharedBackend.RETRY_SECONDS)
    limiter.attempt("u")
    assert sum(r.levelno == logging.WARNING for r in security_log) == 2  # a new outage warns again at once


def test_counts_are_not_carried_across_the_switch(clock):
    """The documented limitation: an outage starts every worker from zero."""
    store = StandInStore()
    limiter = RequestRateLimiter("checkout", 2, 60, backend=backend_with(store))
    limiter.attempt("u")
    limiter.attempt("u")
    assert not limiter.attempt("u").allowed
    store.down = True
    clock.advance(SharedBackend.RETRY_SECONDS)
    assert limiter.attempt("u").allowed


def test_reset_clears_the_fallback_counters_too(clock):
    store = StandInStore()
    limiter = FailureRateLimiter("login", 5, 900, backend=backend_with(store))
    store.down = True
    for _ in range(5):
        limiter.record_failure("k")
    assert limiter.is_limited("k")
    limiter.reset("k")
    assert not limiter.is_limited("k")


def test_an_unreachable_redis_falls_back_quickly_without_logging_the_url(security_log):
    """The real client against a port nothing listens on: bounded by the
    socket timeout, not by the operating system's connect timeout."""
    backend = SharedBackend()
    backend.configure("redis://:do-not-log-me@127.0.0.1:1/0", secret="s", prefix="qj:test", timeout=0.2)
    limiter = RequestRateLimiter("checkout", 2, 60, backend=backend)
    started = time.monotonic()
    decisions = [limiter.attempt("u") for _ in range(3)]
    elapsed = time.monotonic() - started
    assert [d.allowed for d in decisions] == [True, True, False]
    assert not any(d.shared for d in decisions)
    assert elapsed < 1.5, f"fallback took {elapsed:.2f}s"
    assert backend.status()["degraded"] is True
    text = " ".join(r.getMessage() for r in security_log)
    assert "policy=checkout" in text
    assert "do-not-log-me" not in text and "127.0.0.1" not in text
    backend.configure(None, secret="s")


def test_configure_does_not_connect():
    backend = SharedBackend()
    backend.configure("redis://127.0.0.1:1/0", secret="s")
    assert backend.status() == {"backend": "redis", "shared": True, "degraded": False,
                                "degraded_since": None, "last_error": None}
    backend.configure(None, secret="s")
    assert backend.status()["backend"] == "memory"


def test_redis_keys_never_contain_the_identifier():
    backend = SharedBackend()
    backend.configure("redis://127.0.0.1:1/0", secret="server-secret", prefix="qj:rl")
    key = backend._store.key("login", "203.0.113.9:alice")
    assert key.startswith("qj:rl:login:") and len(key.rsplit(":", 1)[1]) == 64
    assert "alice" not in key and "203.0.113.9" not in key
    assert key == backend._store.key("login", "203.0.113.9:alice")  # every worker derives the same key
    assert key != backend._store.key("login", "203.0.113.9:alicf")
    other = SharedBackend()
    other.configure("redis://127.0.0.1:1/0", secret="another-secret", prefix="qj:rl")
    assert key != other._store.key("login", "203.0.113.9:alice")  # keyed with SECRET_KEY
    backend.configure(None, secret="s")
    other.configure(None, secret="s")


# --- application wiring and configuration ------------------------------------------


def test_the_test_application_never_uses_redis(app):
    assert app.config["REDIS_URL"] is None
    assert rate_limit_status() == {"backend": "memory", "shared": False, "degraded": False,
                                   "degraded_since": None, "last_error": None}


def test_testing_config_ignores_redis_url_from_the_environment():
    env = dict(os.environ, REDIS_URL="redis://127.0.0.1:6379/0")
    code = "import json, config; print(json.dumps([config.BaseConfig.REDIS_URL, config.TestingConfig.REDIS_URL]))"
    out = subprocess.run([sys.executable, "-c", code], cwd=BASE_DIR, env=env, capture_output=True, text=True,
                         check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == ["redis://127.0.0.1:6379/0", None]


@pytest.mark.parametrize("url,expected", [
    ("redis://127.0.0.1:6379/0", True),
    ("redis://:password@10.0.0.5:6379/1", True),
    ("redis://app-user:password@redis.internal/0", True),
    ("rediss://redis.internal:6380/0", True),
    ("unix:///run/redis/redis.sock?db=0", True),
    ("http://127.0.0.1:6379", False),
    ("memcached://127.0.0.1", False),
    ("redis://", False),
    ("redis://127.0.0.1:notaport/0", False),
    ("unix://", False),
    ("127.0.0.1:6379", False),
    ("", False),
    (None, False),
])
def test_redis_url_is_supported(url, expected):
    assert redis_url_is_supported(url) is expected


def test_an_unsupported_redis_url_is_refused_without_echoing_it(monkeypatch):
    monkeypatch.setattr(TestingConfig, "REDIS_URL", "http://user:do-not-print-me@cache.internal:6379")
    with pytest.raises(ConfigError, match="REDIS_URL") as excinfo:
        TestingConfig.validate()
    assert "do-not-print-me" not in str(excinfo.value) and "cache.internal" not in str(excinfo.value)


@pytest.mark.parametrize("value", [0, 49, 2001])
def test_redis_socket_timeout_is_bounded(monkeypatch, value):
    monkeypatch.setattr(TestingConfig, "REDIS_SOCKET_TIMEOUT_MS", value)
    with pytest.raises(ConfigError, match="REDIS_SOCKET_TIMEOUT_MS"):
        TestingConfig.validate()


def test_redis_defaults():
    assert BaseConfig.REDIS_SOCKET_TIMEOUT_MS == int(os.environ.get("REDIS_SOCKET_TIMEOUT_MS", "200"))
    assert BaseConfig.RATELIMIT_KEY_PREFIX == "qj:rl"


class _Production(ProductionConfig):
    SECRET_KEY = "x" * 64
    SQLALCHEMY_DATABASE_URI = "mysql+pymysql://qj_user:pw@127.0.0.1/quick_junction"
    REDIS_SOCKET_TIMEOUT_MS = 200


def test_production_requires_redis_url():
    class NoRedis(_Production):
        REDIS_URL = None

    with pytest.raises(ConfigError, match="REDIS_URL"):
        NoRedis.validate()


@pytest.mark.parametrize("url", ["redis://127.0.0.1:6379/0", "rediss://:pw@redis.internal:6380/0",
                                 "unix:///run/redis/redis.sock"])
def test_production_accepts_a_supported_redis_url_with_or_without_credentials(url):
    class WithRedis(_Production):
        REDIS_URL = url

    WithRedis.validate()


def test_production_refuses_an_unsupported_redis_scheme():
    class BadRedis(_Production):
        REDIS_URL = "http://redis.internal:6379"

    with pytest.raises(ConfigError, match="REDIS_URL"):
        BadRedis.validate()
