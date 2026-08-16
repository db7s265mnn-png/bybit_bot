from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from trading_bot.core.exceptions import RateLimitError

T = TypeVar("T")


class TokenBucketRateLimiter:
    """Process-wide limiter so REST calls are centralized, not scattered."""

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_second)
        self._tokens = self._capacity
        self._updated_at = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = 30.0) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                missing = tokens - self._tokens
                wait_for = missing / self._rate
            if deadline is not None and time.monotonic() + wait_for > deadline:
                raise RateLimitError("local rate limiter timed out waiting for a token")
            time.sleep(min(wait_for, 0.05))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated_at
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated_at = now


def with_limit(limiter: TokenBucketRateLimiter, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    limiter.acquire()
    return fn(*args, **kwargs)
