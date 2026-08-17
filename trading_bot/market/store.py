from __future__ import annotations

from trading_bot.database.database import Database
from trading_bot.exchange.instruments import Instrument
from trading_bot.market.candles import Candle
from trading_bot.market.market_data import MarketDataService


class CandleStore:
    def __init__(self, database: Database, market: MarketDataService | None = None) -> None:
        self._db = database
        self._market = market

    def sync(
        self,
        symbol: str,
        timeframe: str,
        *,
        limit: int = 1000,
        instrument: Instrument | None = None,
    ) -> list[Candle]:
        if self._market is None:
            raise RuntimeError("CandleStore.sync requires a MarketDataService")
        extra = 1
        candles = self._market.candles(symbol, timeframe, limit=limit + extra, include_unclosed=False)
        if len(candles) > limit:
            candles = candles[-limit:]
        self._db.upsert_candles(candles)
        if instrument is not None:
            self._db.save_instrument(instrument)
        return candles

    def load(self, symbol: str, timeframe: str) -> list[Candle]:
        from trading_bot.market.candles import to_bybit_interval

        return self._db.load_candles(symbol, to_bybit_interval(timeframe))
