from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_bot.market.candles import Candle


def ema(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    alpha = Decimal("2") / (Decimal(period) + Decimal("1"))
    out: list[Decimal | None] = [None] * len(values)
    seed: Decimal | None = None
    running: Decimal | None = None
    window: list[Decimal] = []
    for i, price in enumerate(values):
        window.append(price)
        if len(window) < period:
            continue
        if seed is None:
            seed = sum(window[-period:], Decimal("0")) / Decimal(period)
            running = seed
            out[i] = running
            continue
        assert running is not None
        running = running + alpha * (price - running)
        out[i] = running
    return out


def true_range(high: Decimal, low: Decimal, prev_close: Decimal) -> Decimal:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(candles: Sequence[Candle], period: int) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("ATR period must be positive")
    out: list[Decimal | None] = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    trs: list[Decimal] = []
    for i in range(1, len(candles)):
        trs.append(true_range(candles[i].high, candles[i].low, candles[i - 1].close))
        idx = i
        if len(trs) < period:
            continue
        if len(trs) == period:
            out[idx] = sum(trs[-period:], Decimal("0")) / Decimal(period)
        else:
            prev = out[idx - 1]
            assert prev is not None
            out[idx] = (prev * Decimal(period - 1) + trs[-1]) / Decimal(period)
    return out
