from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trading_bot.exchange.instruments import to_decimal

TIMEFRAME_TO_INTERVAL: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
}

INTERVAL_MS: dict[str, int] = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,
    "360": 21_600_000,
    "720": 43_200_000,
    "D": 86_400_000,
    "W": 604_800_000,
}


def to_bybit_interval(timeframe: str) -> str:
    if timeframe in INTERVAL_MS:
        return timeframe
    if timeframe not in TIMEFRAME_TO_INTERVAL:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    return TIMEFRAME_TO_INTERVAL[timeframe]


def interval_to_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval!r}")
    return INTERVAL_MS[interval]


@dataclass(frozen=True)
class Candle:
    symbol: str
    interval: str
    start_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    confirmed: bool

    @property
    def start_ms(self) -> int:
        return int(self.start_time.timestamp() * 1000)


def parse_kline_row(symbol: str, interval: str, row: list[Any], *, now_ms: int) -> Candle:
    start_ms = int(row[0])
    duration = interval_to_ms(interval)
    close_ms = start_ms + duration
    # A candle is only confirmed after its period has ended. The still-open bar's
    # "close" is the last traded price and must not be treated as a known close.
    confirmed = close_ms <= now_ms
    return Candle(
        symbol=symbol,
        interval=interval,
        start_time=datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
        open=to_decimal(row[1]),
        high=to_decimal(row[2]),
        low=to_decimal(row[3]),
        close=to_decimal(row[4]),
        volume=to_decimal(row[5]),
        turnover=to_decimal(row[6] if len(row) > 6 else "0"),
        confirmed=confirmed,
    )


def parse_ws_kline_item(symbol: str, interval: str, item: dict[str, Any]) -> Candle:
    """Parse one Bybit public kline WebSocket row (`confirm` marks a closed bar)."""
    start_ms = int(item["start"])
    row_interval = str(item.get("interval") or interval)
    return Candle(
        symbol=symbol,
        interval=row_interval,
        start_time=datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
        open=to_decimal(item["open"]),
        high=to_decimal(item["high"]),
        low=to_decimal(item["low"]),
        close=to_decimal(item["close"]),
        volume=to_decimal(item.get("volume") or "0"),
        turnover=to_decimal(item.get("turnover") or "0"),
        confirmed=bool(item.get("confirm")),
    )


def parse_ws_kline_message(payload: dict[str, Any]) -> list[Candle]:
    topic = str(payload.get("topic") or "")
    parts = topic.split(".")
    interval = parts[1] if len(parts) >= 3 else ""
    symbol = parts[2] if len(parts) >= 3 else ""
    candles: list[Candle] = []
    for item in payload.get("data") or []:
        item_symbol = str(item.get("symbol") or symbol)
        candles.append(parse_ws_kline_item(item_symbol, interval, item))
    return candles


def parse_kline_payload(
    payload: dict[str, Any],
    *,
    interval: str,
    now_ms: int,
    include_unclosed: bool = False,
) -> list[Candle]:
    result = payload.get("result") or {}
    symbol = str(result.get("symbol") or "")
    rows = list(result.get("list") or [])
    # Bybit returns newest-first. Strategies must see chronological order.
    rows.reverse()
    candles = [parse_kline_row(symbol, interval, row, now_ms=now_ms) for row in rows]
    if not include_unclosed:
        candles = [candle for candle in candles if candle.confirmed]
    return candles
