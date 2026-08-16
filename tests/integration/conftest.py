from __future__ import annotations

import pytest
import requests

_PROBE_URL = "https://api-testnet.bybit.com/v5/market/time"


def bybit_skip_reason() -> str | None:
    try:
        response = requests.get(_PROBE_URL, timeout=8)
    except requests.RequestException as exc:
        return f"Bybit unreachable: {exc}"
    body = response.text.lower()
    if response.status_code == 403 or "from your country" in body or "cloudfront" in body:
        return (
            "Bybit CloudFront geo-blocked this environment (HTTP "
            f"{response.status_code}). Public REST/WS integration tests need a non-restricted IP."
        )
    if response.status_code != 200:
        return f"Bybit public API HTTP {response.status_code}"
    return None


@pytest.fixture(scope="session")
def require_bybit() -> None:
    reason = bybit_skip_reason()
    if reason:
        pytest.skip(reason)
