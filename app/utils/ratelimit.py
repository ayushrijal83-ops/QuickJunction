"""Rate limiting: a shared (Redis) sliding window with a per-process fallback.

Two different things are limited, and they are kept as two APIs on purpose:

``FailureRateLimiter``  counts *failed* attempts (login, wrong current
                        password). The routes use ``try_reserve`` (an
                        atomic check-and-record), then ``reset`` when the
                        secret was right, ``release`` when no verdict was
                        reached, and nothing when it was wrong -- the
                        reservation stands as the recorded failure. The older
                        ``is_limited`` / ``record_failure`` pair still works
                        but is not atomic (see docs/SECURITY.md 22.5).
``RequestRateLimiter``  counts *requests*. One call, ``attempt``, checks the
                        limit and records the request atomically.

Every policy's numbers come from ``RATE_LIMITS`` in config.py.

Where the counters live:

* ``REDIS_URL`` set -- in Redis, one sorted set per (policy, identifier),
  changed only by the Lua scripts below, so every worker and every Redis
  client sees and enforces the same window.
* ``REDIS_URL`` unset (development, tests) -- in this process's memory,
  exactly as before S1-A.
* Redis configured but unreachable -- in this process's memory until Redis
  answers again. That is weaker: each worker counts alone, and counts are not
  carried across the switch in either direction. It is logged (a WARNING per
  policy, about once a minute) and reported by ``rate_limit_status()``; a
  decision made this way has ``shared=False``.

Identifiers (IP addresses, usernames, user ids) never appear in a Redis key:
the key holds an HMAC-SHA256 of the identifier, keyed with ``SECRET_KEY``.
Nothing here logs an identifier, a Redis URL or an exception message.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import NamedTuple

from app.utils.logging import get_security_logger
from config import BaseConfig

_POLICY_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# Each script prunes, then reads or writes, as one atomic unit: Redis runs a
# script to completion before serving any other client. Time comes from the
# Redis server (TIME), in microseconds, so workers with different clocks
# still share one window. ARGV[1] is the window in microseconds.
_PRUNE = """
local t = redis.call('TIME')
local now = t[1] * 1000000 + t[2]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[1]))
"""

# Check and record in one step. ARGV[2] = limit, ARGV[3] = unique member.
# A refused request is not recorded, so being refused never extends the window.
# When refused, the third value is how long (microseconds) until the oldest
# entry leaves the window -- the earliest a slot can free up (Retry-After).
_HIT_LUA = _PRUNE + """
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, count, tonumber(oldest[2]) + tonumber(ARGV[1]) - now}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[1]) / 1000))
return {1, count + 1}
"""

# Record unconditionally (a failure that has already happened). ARGV[2] = member.
_ADD_LUA = _PRUNE + """
redis.call('ZADD', KEYS[1], now, ARGV[2])
redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[1]) / 1000))
return redis.call('ZCARD', KEYS[1])
"""

_COUNT_LUA = _PRUNE + """
return redis.call('ZCARD', KEYS[1])
"""


def _now() -> float:
    return time.monotonic()


class _Hit(NamedTuple):
    allowed: bool
    count: int
    retry_after: float | None  # seconds until a slot frees up; refused only
    token: str | None  # the entry recorded for this call; allowed only


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    count: int  # entries in the window after this call
    limit: int
    shared: bool  # True only when Redis made the decision
    # Refused: seconds until the oldest entry leaves the window (Retry-After).
    retry_after: float | None = None
    # Allowed: identifies the entry this call recorded, for ``release``.
    token: str | None = None


class _MemoryWindow:
    """Sliding window in this process. Each method is atomic under the lock.

    One window may hold several policies' keys (the password-change limiter
    shares the login limiter's), so callers pass their own limit and window;
    the constructor's are only defaults."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        # key -> [(time recorded, entry token), ...], oldest first
        self._attempts: dict[str, list[tuple[float, str]]] = {}
        self._lock = threading.Lock()

    def _pruned(self, key: str, now: float, window: float) -> list[tuple[float, str]]:
        cutoff = now - window
        attempts = [entry for entry in self._attempts.get(key, []) if entry[0] > cutoff]
        if attempts:
            self._attempts[key] = attempts
        else:
            self._attempts.pop(key, None)
        return attempts

    def count(self, key: str, window: float | None = None) -> int:
        with self._lock:
            return len(self._pruned(key, _now(), window or self._window))

    def add(self, key: str, window: float | None = None) -> int:
        with self._lock:
            now = _now()
            attempts = self._pruned(key, now, window or self._window)
            attempts.append((now, secrets.token_hex(8)))
            self._attempts[key] = attempts
            return len(attempts)

    def hit(self, key: str, limit: int | None = None, window: float | None = None) -> _Hit:
        limit = limit or self._limit
        window = window or self._window
        with self._lock:
            now = _now()
            attempts = self._pruned(key, now, window)
            if len(attempts) >= limit:
                return _Hit(False, len(attempts), attempts[0][0] + window - now, None)
            token = secrets.token_hex(8)
            attempts.append((now, token))
            self._attempts[key] = attempts
            return _Hit(True, len(attempts), None, token)

    def release(self, key: str, token: str) -> None:
        with self._lock:
            attempts = [entry for entry in self._attempts.get(key, []) if entry[1] != token]
            if attempts:
                self._attempts[key] = attempts
            else:
                self._attempts.pop(key, None)

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._attempts.clear()


class _RedisWindowStore:
    def __init__(self, client, prefix: str, secret: str, errors: tuple[type[BaseException], ...]) -> None:
        self._client = client
        self._prefix = prefix
        self._secret = secret.encode("utf-8")
        self.errors = errors
        self._hit = client.register_script(_HIT_LUA)
        self._add = client.register_script(_ADD_LUA)
        self._count = client.register_script(_COUNT_LUA)

    def key(self, policy: str, identifier: str) -> str:
        digest = hmac.new(self._secret, identifier.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
        return f"{self._prefix}:{policy}:{digest}"

    @staticmethod
    def _window_us(window_seconds: float) -> int:
        return int(window_seconds * 1_000_000)

    def hit(self, policy: str, identifier: str, limit: int, window_seconds: float) -> _Hit:
        # The member is random per request, so two requests in the same
        # microsecond are two entries, never one overwriting the other. It is
        # also the reservation token ``release`` removes.
        token = secrets.token_hex(16)
        result = self._hit(
            keys=[self.key(policy, identifier)],
            args=[self._window_us(window_seconds), limit, token],
        )
        if int(result[0]):
            return _Hit(True, int(result[1]), None, token)
        return _Hit(False, int(result[1]), max(int(result[2]), 0) / 1_000_000, None)

    def release(self, policy: str, identifier: str, token: str) -> None:
        # One member of one sorted set: a single, atomic command.
        self._client.zrem(self.key(policy, identifier), token)

    def add(self, policy: str, identifier: str, window_seconds: float) -> int:
        return int(self._add(keys=[self.key(policy, identifier)],
                             args=[self._window_us(window_seconds), secrets.token_hex(16)]))

    def count(self, policy: str, identifier: str, window_seconds: float) -> int:
        return int(self._count(keys=[self.key(policy, identifier)], args=[self._window_us(window_seconds)]))

    def reset(self, policy: str, identifier: str) -> None:
        self._client.delete(self.key(policy, identifier))

    def close(self) -> None:
        try:
            self._client.close()
        except self.errors:
            pass


class SharedBackend:
    """One process's connection to the shared store, and its health.

    ``run`` tries Redis once, within the socket timeout. On failure it stops
    trying for ``RETRY_SECONDS`` (so an outage costs one timeout every few
    seconds, not one per request) and the caller's in-memory fallback is used
    instead.
    """

    RETRY_SECONDS = 5.0
    WARNING_SECONDS = 60.0

    def __init__(self) -> None:
        self._store: _RedisWindowStore | None = None
        self._lock = threading.Lock()
        self._retry_at = 0.0
        self._degraded_since: float | None = None
        self._last_error: str | None = None
        self._last_warning: dict[str, float] = {}

    def configure(self, url: str | None, *, secret: str, prefix: str = "qj:rl", timeout: float = 0.2) -> None:
        """Point at ``url`` (or at nothing, for in-memory only). Does not
        connect: the first limiter call does."""
        with self._lock:
            if self._store is not None:
                self._store.close()
            self._store = None
            self._retry_at = 0.0
            self._degraded_since = None
            self._last_error = None
            self._last_warning = {}
            if not url:
                return
            import redis  # only needed when a shared store is configured
            from redis.backoff import NoBackoff
            from redis.retry import Retry

            client = redis.Redis.from_url(
                url,
                socket_timeout=timeout,
                socket_connect_timeout=timeout,
                retry=Retry(NoBackoff(), 0),  # one bounded attempt; the fallback is the retry
                decode_responses=True,
            )
            self._store = _RedisWindowStore(client, prefix, secret, (redis.RedisError, OSError))

    def run(self, policy: str, operation, fallback):
        """``(result, shared)``: ``operation(store)`` from Redis, else ``fallback()``."""
        store = self._store
        if store is None:
            return fallback(), False
        if _now() < self._retry_at:
            self._warn(policy)
            return fallback(), False
        try:
            result = operation(store)
        except store.errors as exc:
            self._mark_down(exc)
            self._warn(policy)
            return fallback(), False
        self._mark_up()
        return result, True

    def _mark_down(self, exc: BaseException) -> None:
        with self._lock:
            self._retry_at = _now() + self.RETRY_SECONDS
            self._last_error = type(exc).__name__
            if self._degraded_since is None:
                self._degraded_since = time.time()

    def _mark_up(self) -> None:
        if self._degraded_since is None:
            return
        with self._lock:
            if self._degraded_since is None:
                return
            self._degraded_since = None
            self._last_error = None
            self._last_warning = {}
        get_security_logger().info("rate limiting restored: the shared store is reachable again")

    def _warn(self, policy: str) -> None:
        now = _now()
        with self._lock:
            last = self._last_warning.get(policy)
            if last is not None and now - last < self.WARNING_SECONDS:
                return
            self._last_warning[policy] = now
            error = self._last_error
        # The error's class name only: a Redis error message can carry the host.
        get_security_logger().warning(
            "rate limiting degraded: policy=%s shared store unavailable (%s); using per-process "
            "in-memory limits -- limits are NOT shared across workers", policy, error)

    def status(self) -> dict:
        with self._lock:
            configured = self._store is not None
            degraded = self._degraded_since is not None
            return {
                "backend": "redis" if configured else "memory",
                "shared": configured and not degraded,
                "degraded": degraded,
                "degraded_since": self._degraded_since,
                "last_error": self._last_error,
            }


class _WindowLimiter:
    def __init__(self, name: str, limit: int, window_seconds: float, *, backend: SharedBackend | None,
                 local: _MemoryWindow | None = None) -> None:
        if not _POLICY_NAME.match(name):
            raise ValueError("rate-limit policy names are lowercase letters, digits and underscores")
        if limit < 1 or window_seconds <= 0:
            raise ValueError("a rate limit needs a positive limit and window")
        self.name = name
        self.limit = limit
        self.window_seconds = window_seconds
        self._backend = backend
        self._local = local or _MemoryWindow(limit, window_seconds)

    def configure(self, limit: int, window_seconds: float) -> None:
        """Apply this policy's numbers from configuration."""
        if limit < 1 or window_seconds <= 0:
            raise ValueError("a rate limit needs a positive limit and window")
        self.limit = limit
        self.window_seconds = window_seconds

    def _run(self, operation, fallback):
        if self._backend is None:
            return fallback(), False
        return self._backend.run(self.name, operation, fallback)

    def _hit(self, key: str) -> RateLimitDecision:
        hit, shared = self._run(
            lambda store: store.hit(self.name, key, self.limit, self.window_seconds),
            lambda: self._local.hit(key, self.limit, self.window_seconds),
        )
        return RateLimitDecision(allowed=hit.allowed, count=hit.count, limit=self.limit, shared=shared,
                                 retry_after=hit.retry_after, token=hit.token)

    def reset(self, key: str) -> None:
        self._run(lambda store: store.reset(self.name, key), lambda: None)
        self._local.reset(key)  # also drops anything counted during an outage


class RequestRateLimiter(_WindowLimiter):
    """At most ``limit`` requests per ``window_seconds`` per identifier."""

    def attempt(self, identifier: str) -> RateLimitDecision:
        """Check the limit and record this request, atomically. A refused
        request is not recorded."""
        return self._hit(identifier)


class FailureRateLimiter(_WindowLimiter):
    """At most ``limit`` failed attempts per ``window_seconds`` per key."""

    def is_limited(self, key: str) -> bool:
        count, _ = self._run(lambda store: store.count(self.name, key, self.window_seconds),
                             lambda: self._local.count(key, self.window_seconds))
        return count >= self.limit

    def record_failure(self, key: str) -> None:
        self._run(lambda store: store.add(self.name, key, self.window_seconds),
                  lambda: self._local.add(key, self.window_seconds))

    def try_reserve(self, key: str) -> RateLimitDecision:
        """Atomic alternative to ``is_limited`` + ``record_failure``: count
        this attempt as a failure *before* it is checked, unless the limit is
        already reached, so attempts in flight at the same time hold a slot
        each and cannot together exceed the limit in the store that decided
        (Redis: across workers; during an outage: per worker only). Then
        exactly one of:

        * the secret was wrong -- do nothing: the reservation is the failure;
        * the secret was right -- ``reset`` (clears the key, as it always has);
        * no verdict (an error, or a rejection that is not a wrong secret) --
          ``release(key, decision.token)`` removes this reservation only.

        A reservation nobody resolves (the worker died) is an ordinary entry:
        it leaves the window ``window_seconds`` later, like any failure.
        See docs/SECURITY.md section 22.5."""
        return self._hit(key)

    def release(self, key: str, token: str | None) -> None:
        """Withdraw one reservation made by ``try_reserve``, leaving every
        other entry (other attempts, in flight or failed) untouched."""
        if token is None:
            return
        self._run(lambda store: store.release(self.name, key, token), lambda: None)
        self._local.release(key, token)  # it may have been made during an outage

    @property
    def _attempts(self) -> dict[str, list[tuple[float, str]]]:
        """The in-memory counters (what tests clear between cases)."""
        return self._local._attempts


_default_backend = SharedBackend()


def _policy(name: str) -> tuple[int, int]:
    return BaseConfig.RATE_LIMITS[name]


# Keyed by f"{client ip}:{normalized username}".
login_limiter = FailureRateLimiter("login", *_policy("login"), backend=_default_backend)

# Keyed by f"password-change:{user id}". Shares the login limiter's in-memory
# store (its keys cannot collide: every login key starts with an IP), so the
# existing tests' ``login_limiter._attempts.clear()`` still resets both.
password_change_limiter = FailureRateLimiter("password_change", *_policy("password_change"),
                                             backend=_default_backend, local=login_limiter._local)

# Keyed by client IP only -- never by a submitted username, email or code, so
# rotating them does not reset the count and nobody can exhaust another
# person's allowance by naming them.
registration_limiter = RequestRateLimiter("registration", *_policy("registration"), backend=_default_backend)
account_setup_limiter = RequestRateLimiter("account_setup", *_policy("account_setup"), backend=_default_backend)

# Keyed by the authenticated user's id (from the server-side session).
checkout_limiter = RequestRateLimiter("checkout", *_policy("checkout"), backend=_default_backend)
reservation_limiter = RequestRateLimiter("reservation", *_policy("reservation"), backend=_default_backend)
ai_explanation_limiter = RequestRateLimiter("ai_explanation", *_policy("ai_explanation"), backend=_default_backend)

POLICIES: dict[str, _WindowLimiter] = {
    limiter.name: limiter
    for limiter in (login_limiter, password_change_limiter, registration_limiter, account_setup_limiter,
                    checkout_limiter, reservation_limiter, ai_explanation_limiter)
}


def reset_local_counters() -> None:
    """Forget every in-memory count (all policies). For tests; the shared
    store is not touched."""
    for limiter in POLICIES.values():
        limiter._local.clear()


def init_rate_limiting(app) -> None:
    for name, (limit, window_seconds) in app.config["RATE_LIMITS"].items():
        POLICIES[name].configure(limit, window_seconds)
    _default_backend.configure(
        app.config.get("REDIS_URL"),
        secret=app.config["SECRET_KEY"],
        prefix=app.config["RATELIMIT_KEY_PREFIX"],
        timeout=app.config["REDIS_SOCKET_TIMEOUT_MS"] / 1000,
    )
    if app.config.get("REDIS_URL"):
        app.logger.info("Rate limiting: shared store configured (Redis)")
    else:
        app.logger.info("Rate limiting: in-memory, per process (REDIS_URL is not set)")


def rate_limit_status() -> dict:
    """This process's view: ``backend`` (what is configured), ``shared``
    (whether limits are currently enforced across workers), ``degraded``."""
    return _default_backend.status()
