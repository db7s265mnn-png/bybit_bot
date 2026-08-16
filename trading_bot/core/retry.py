from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from trading_bot.core.exceptions import (
    AuthenticationError,
    BybitAPIError,
    GeoRestrictedError,
    RateLimitError,
    WithdrawPermissionError,
)

T = TypeVar("T")

# Do not retry auth / permission / validation failures.
_NON_RETRYABLE = (
    AuthenticationError,
    WithdrawPermissionError,
    GeoRestrictedError,
)

_RETRYABLE_RET_CODES = {
    10000,  # server timeout
    10002,  # recv_window / clock
    10006,  # API rate limit
    10016,  # internal server error
    10018,  # IP rate limit
    10429,  # frequency protection
    429,
}


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _NON_RETRYABLE):
        return False
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, BybitAPIError):
        if exc.ret_code in _RETRYABLE_RET_CODES:
            return True
        if exc.status_code in {408, 429, 500, 502, 503, 504}:
            return True
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__
    return name in {
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectionError",
        "SSLError",
        "ChunkedEncodingError",
        "FailedRequestError",
    }


def retrying(
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    before_sleep: Callable[[RetryCallState], None] | None = None,
):
    """Exponential backoff with jitter. Attempts are finite; never an infinite loop."""

    return retry(
        reraise=True,
        stop=stop_after_attempt(max(1, max_attempts)),
        wait=wait_exponential_jitter(initial=base_delay, max=max_delay),
        retry=retry_if_exception(is_retryable),
        before_sleep=before_sleep,
    )


def sleep_backoff(attempt: int, base: float, cap: float) -> float:
    delay = min(cap, base * (2 ** max(0, attempt)))
    jitter = random.uniform(0, delay * 0.2)
    total = delay + jitter
    time.sleep(total)
    return total
