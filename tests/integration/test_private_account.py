from __future__ import annotations

import os

import pytest

from trading_bot.config.loader import load_config
from trading_bot.account.service import AccountService
from trading_bot.exchange.bybit_client import BybitRESTClient


@pytest.mark.integration
@pytest.mark.private
def test_private_account_snapshot() -> None:
    if not os.getenv("BYBIT_API_KEY") or not os.getenv("BYBIT_API_SECRET"):
        pytest.skip("BYBIT_API_KEY / BYBIT_API_SECRET not set")
    config = load_config()
    client = BybitRESTClient(config)
    snapshot = AccountService(client).snapshot()
    assert snapshot.api_key.has_withdraw is False
    assert snapshot.wallet.account_type
