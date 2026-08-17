from __future__ import annotations

from datetime import datetime, timezone

from pybit.exceptions import FailedRequestError

from trading_bot.core.exceptions import GeoRestrictedError
from trading_bot.core.retry import is_retryable
from trading_bot.exchange.bybit_client import BybitRESTClient, _map_pybit_error
from trading_bot.market.market_data import MarketDataService


class FakeHTTP:
    def __init__(self, instrument: dict) -> None:
        self.instrument = instrument

    def get_server_time(self) -> dict:
        return {
            "retCode": 0,
            "result": {"timeSecond": "1700000000", "timeNano": "1700000000123456789"},
        }

    def get_instruments_info(self, **kwargs) -> dict:
        return {"retCode": 0, "result": {"list": [self.instrument]}}

    def get_kline(self, **kwargs) -> dict:
        start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        step = 15 * 60 * 1000
        newest = [str(start + step), "2", "2", "2", "2", "1", "1"]
        older = [str(start), "1", "1", "1", "1", "1", "1"]
        return {"retCode": 0, "result": {"symbol": kwargs["symbol"], "list": [newest, older]}}

    def get_tickers(self, **kwargs) -> dict:
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": kwargs.get("symbol") or "BTCUSDT",
                        "lastPrice": "100",
                        "bid1Price": "99.9",
                        "ask1Price": "100.1",
                    }
                ]
            },
        }

    def get_orderbook(self, **kwargs) -> dict:
        return {
            "retCode": 0,
            "result": {
                "s": "BTCUSDT",
                "b": [["99.9", "1"]],
                "a": [["100.1", "1"]],
                "ts": 1,
            },
        }


def test_rest_client_uses_injected_http(app_config, btc_instrument_payload) -> None:
    client = BybitRESTClient(app_config, http=FakeHTTP(btc_instrument_payload))
    instrument = client.get_instrument("BTCUSDT")
    assert instrument.tick_size > 0
    sync = client.get_server_time()
    assert sync.server_time_ms > 0


def test_market_data_with_injected_http(app_config, btc_instrument_payload) -> None:
    market = MarketDataService(BybitRESTClient(app_config, http=FakeHTTP(btc_instrument_payload)))
    now_ms = int(datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
    candles = market.candles("BTCUSDT", "15m", limit=10, now_ms=now_ms)
    assert candles
    assert all(c.confirmed for c in candles)
    ticker = market.ticker("BTCUSDT")
    assert ticker.spread is not None


def test_geo_block_is_not_retried() -> None:
    mapped = _map_pybit_error(
        FailedRequestError(
            request="GET /v5/market/time",
            message="The Amazon CloudFront distribution is configured to block access from your country.",
            status_code=403,
            time="00:00:00",
            resp_headers=None,
        )
    )
    assert isinstance(mapped, GeoRestrictedError)
    assert is_retryable(mapped) is False
    assert "blocked this IP/country" in str(mapped)
