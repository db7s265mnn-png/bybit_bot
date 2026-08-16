from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.market.candles import Candle


def make_candles(
    closes: list[Decimal | int | float | str],
    *,
    symbol: str = "BTCUSDT",
    interval: str = "15",
    start: datetime | None = None,
    step_ms: int = 900_000,
) -> list[Candle]:
    origin = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    origin_ms = int(origin.timestamp() * 1000)
    candles: list[Candle] = []
    prev = Decimal(str(closes[0]))
    for i, raw in enumerate(closes):
        close = Decimal(str(raw))
        high = max(prev, close) * Decimal("1.001")
        low = min(prev, close) * Decimal("0.999")
        open_ = prev
        candles.append(
            Candle(
                symbol=symbol,
                interval=interval,
                start_time=datetime.fromtimestamp((origin_ms + i * step_ms) / 1000, tz=timezone.utc),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal("10"),
                turnover=Decimal("1000"),
                confirmed=True,
            )
        )
        prev = close
    return candles
