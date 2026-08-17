"""Login attempt rate limiting.

ponytail: in-memory, single-process, resets on restart or across gunicorn
workers. That is the ceiling of a dict-and-lock limiter. It is real
protection for a single dev/staging process; a multi-worker or
multi-instance production deployment needs a shared store (Flask-Limiter
with a Redis backend is the standard choice) so workers share counters.
Not added now because it is a new dependency this milestone does not need
to prove the concept -- swap ``_LoginAttemptLimiter`` for it without
touching callers, both expose the same three methods.
"""

from __future__ import annotations

import threading
import time


class _LoginAttemptLimiter:
    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        attempts = self._attempts.get(key, [])
        cutoff = now - self._window
        return [t for t in attempts if t > cutoff]

    def is_limited(self, key: str) -> bool:
        with self._lock:
            attempts = self._prune(key, time.monotonic())
            self._attempts[key] = attempts
            return len(attempts) >= self._max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            attempts = self._prune(key, now)
            attempts.append(now)
            self._attempts[key] = attempts

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


# 5 failures per 15 minutes per (ip, normalized-username) pair.
login_limiter = _LoginAttemptLimiter(max_attempts=5, window_seconds=15 * 60)
