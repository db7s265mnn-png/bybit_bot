from __future__ import annotations

from datetime import datetime, timezone

from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.exchange.instruments import Instrument
from trading_bot.market.candles import Candle, parse_kline_payload, to_bybit_interval
from trading_bot.market.orderbook import OrderBook, Ticker, parse_orderbook, parse_ticker


class MarketDataService:
    """Public market data. Uses REST; live streaming is handled by BybitWebSocket."""

    def __init__(self, client: BybitRESTClient) -> None:
        self._client = client

    def server_now_ms(self) -> int:
        sync = self._client.get_server_time()
        return sync.now_ms()

    def instruments(self, symbol: str | None = None) -> list[Instrument]:
        return self._client.get_instruments(symbol)

    def candles(
        self,
        symbol: str,
        timeframe: str,
        *,
        limit: int = 200,
        include_unclosed: bool = False,
        now_ms: int | None = None,
    ) -> list[Candle]:
        """Fetch candles oldest-first.

        By default the still-open bar is dropped so a strategy cannot treat an
        in-progress close as a known event (look-ahead protection).
        """
        interval = to_bybit_interval(timeframe)
        if now_ms is None:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        collected: list[list] = []
        end: int | None = None
        remaining = max(limit, 1)
        while remaining > 0:
            batch = min(remaining, 1000)
            payload = self._client.get_kline(symbol, interval, end=end, limit=batch)
            rows = list((payload.get("result") or {}).get("list") or [])
            if not rows:
                break
            collected.extend(rows)
            oldest_start = int(rows[-1][0])
            end = oldest_start - 1
            remaining -= len(rows)
            if len(rows) < batch:
                break
        wrapped = {
            "result": {
                "symbol": symbol,
                "list": collected,
            }
        }
        candles = parse_kline_payload(
            wrapped,
            interval=interval,
            now_ms=now_ms,
            include_unclosed=include_unclosed,
        )
        if len(candles) > limit:
            candles = candles[-limit:]
        return candles

    def ticker(self, symbol: str) -> Ticker:
        payload = self._client.get_tickers(symbol)
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            raise ValueError(f"no ticker for {symbol}")
        return parse_ticker(rows[0])

    def orderbook(self, symbol: str, limit: int = 25) -> OrderBook:
        payload = self._client.get_orderbook(symbol, limit=limit)
        return parse_orderbook(payload)
