from __future__ import annotations

import time

import pytest

from trading_bot.core.exceptions import AuthenticationError, RateLimitError
from trading_bot.core.retry import is_retryable, retrying
from trading_bot.exchange.rate_limiter import TokenBucketRateLimiter


def test_rate_limiter_allows_configured_rate() -> None:
    limiter = TokenBucketRateLimiter(rate_per_second=100, capacity=2)
    started = time.monotonic()
    limiter.acquire()
    limiter.acquire()
    elapsed = time.monotonic() - started
    assert elapsed < 1.0


def test_retryable_classification() -> None:
    assert is_retryable(RateLimitError("slow down", ret_code=10006))
    assert not is_retryable(AuthenticationError("bad key", ret_code=10003))
    from trading_bot.core.exceptions import InvalidOrderError

    assert not is_retryable(InvalidOrderError("bad qty", ret_code=110001))


def test_retry_succeeds_after_transient_errors() -> None:
    calls = {"n": 0}

    @retrying(max_attempts=3, base_delay=0.01, max_delay=0.02)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("retry me", ret_code=10006)
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_gives_up() -> None:
    @retrying(max_attempts=2, base_delay=0.01, max_delay=0.02)
    def always_fail() -> None:
        raise RateLimitError("nope", ret_code=10006)

    with pytest.raises(RateLimitError):
        always_fail()
