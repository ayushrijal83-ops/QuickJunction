"""Rate limiting: a shared (Redis) sliding window with a per-process fallback.

Two different things are limited, and they are kept as two APIs on purpose:

``FailureRateLimiter``  counts *failed* attempts (login, wrong current
                        password). Callers ask ``is_limited``, do the work,
                        then ``record_failure`` or ``reset`` -- the three
                        methods the login view has always used.
``RequestRateLimiter``  counts *requests*. One call, ``attempt``, checks the
                        limit and records the request atomically.

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

from app.utils.logging import get_security_logger

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
_HIT_LUA = _PRUNE + """
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
  return {0, count}
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


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    count: int  # entries in the window after this call
    limit: int
    shared: bool  # True only when Redis made the decision


class _MemoryWindow:
    """Sliding window in this process. Each method is atomic under the lock."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _pruned(self, key: str, now: float) -> list[float]:
        cutoff = now - self._window
        attempts = [t for t in self._attempts.get(key, []) if t > cutoff]
        if attempts:
            self._attempts[key] = attempts
        else:
            self._attempts.pop(key, None)
        return attempts

    def count(self, key: str) -> int:
        with self._lock:
            return len(self._pruned(key, _now()))

    def add(self, key: str) -> int:
        with self._lock:
            now = _now()
            attempts = self._pruned(key, now)
            attempts.append(now)
            self._attempts[key] = attempts
            return len(attempts)

    def hit(self, key: str) -> tuple[bool, int]:
        with self._lock:
            now = _now()
            attempts = self._pruned(key, now)
            if len(attempts) >= self._limit:
                return False, len(attempts)
            attempts.append(now)
            self._attempts[key] = attempts
            return True, len(attempts)

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


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

    def hit(self, policy: str, identifier: str, limit: int, window_seconds: float) -> tuple[bool, int]:
        # The member is random per request, so two requests in the same
        # microsecond are two entries, never one overwriting the other.
        allowed, count = self._hit(
            keys=[self.key(policy, identifier)],
            args=[self._window_us(window_seconds), limit, secrets.token_hex(16)],
        )
        return bool(allowed), int(count)

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

    def _run(self, operation, fallback):
        if self._backend is None:
            return fallback(), False
        return self._backend.run(self.name, operation, fallback)

    def _hit(self, key: str) -> RateLimitDecision:
        (allowed, count), shared = self._run(
            lambda store: store.hit(self.name, key, self.limit, self.window_seconds),
            lambda: self._local.hit(key),
        )
        return RateLimitDecision(allowed=allowed, count=count, limit=self.limit, shared=shared)

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
                             lambda: self._local.count(key))
        return count >= self.limit

    def record_failure(self, key: str) -> None:
        self._run(lambda store: store.add(self.name, key, self.window_seconds), lambda: self._local.add(key))

    def try_reserve(self, key: str) -> RateLimitDecision:
        """Atomic alternative to ``is_limited`` + ``record_failure``: count
        this attempt as a failure *before* it is checked, unless the limit is
        already reached. The caller then calls ``reset`` on success and does
        nothing on failure. Not used by the login view yet -- see
        docs/SECURITY.md section 22.5."""
        return self._hit(key)

    @property
    def _attempts(self) -> dict[str, list[float]]:
        """The in-memory counters (what tests clear between cases)."""
        return self._local._attempts


_default_backend = SharedBackend()

# 5 failures per 15 minutes per (ip, normalized-username) pair.
login_limiter = FailureRateLimiter("login", 5, 15 * 60, backend=_default_backend)

# 5 wrong current passwords per 15 minutes per account. In memory only, in the
# same store it has always used, until S1-B moves it to the shared backend.
password_change_limiter = FailureRateLimiter("password_change", 5, 15 * 60, backend=None,
                                             local=login_limiter._local)


def init_rate_limiting(app) -> None:
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
