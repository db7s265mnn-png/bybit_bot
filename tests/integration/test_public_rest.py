from __future__ import annotations

import pytest

from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.market.market_data import MarketDataService


@pytest.mark.integration
def test_public_server_time(app_config, require_bybit) -> None:
    client = BybitRESTClient(app_config)
    sync = client.get_server_time()
    assert sync.server_time_ms > 0


@pytest.mark.integration
def test_public_instrument_constraints_come_from_api(app_config, require_bybit) -> None:
    client = BybitRESTClient(app_config)
    instrument = client.get_instrument("BTCUSDT", use_cache=False)
    assert instrument.symbol == "BTCUSDT"
    assert instrument.tick_size > 0
    assert instrument.qty_step > 0
    assert instrument.min_order_qty > 0
    assert instrument.is_trading


@pytest.mark.integration
def test_public_candles_drop_unclosed_by_default(app_config, require_bybit) -> None:
    client = BybitRESTClient(app_config)
    market = MarketDataService(client)
    candles = market.candles("BTCUSDT", app_config.trading.timeframe, limit=10)
    assert len(candles) >= 5
    assert all(c.confirmed for c in candles)
    assert candles[0].start_time < candles[-1].start_time


@pytest.mark.integration
def test_public_ticker_and_orderbook(app_config, require_bybit) -> None:
    market = MarketDataService(BybitRESTClient(app_config))
    ticker = market.ticker("BTCUSDT")
    book = market.orderbook("BTCUSDT", limit=5)
    assert ticker.last_price > 0
    assert book.best_bid is not None
    assert book.best_ask is not None
    assert book.best_ask.price >= book.best_bid.price
