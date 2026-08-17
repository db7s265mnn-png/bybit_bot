from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.config.loader import load_config
from trading_bot.config.models import TradingMode
from trading_bot.monitoring.logger import configure_logging

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _logging() -> None:
    configure_logging("WARNING", log_dir=None)


@pytest.fixture
def config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "config.yaml"


@pytest.fixture
def app_config(config_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("BYBIT_TESTNET", "true")
    return load_config(config_path)


@pytest.fixture
def btc_instrument_payload() -> dict:
    return {
        "symbol": "BTCUSDT",
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "leverageFilter": {
            "minLeverage": "1",
            "maxLeverage": "100.00",
            "leverageStep": "0.01",
        },
        "priceFilter": {
            "minPrice": "0.10",
            "maxPrice": "1999999.80",
            "tickSize": "0.10",
        },
        "lotSizeFilter": {
            "maxOrderQty": "1190.000",
            "minOrderQty": "0.001",
            "qtyStep": "0.001",
            "maxMktOrderQty": "500.000",
            "minNotionalValue": "5",
        },
        "fundingInterval": 480,
    }
