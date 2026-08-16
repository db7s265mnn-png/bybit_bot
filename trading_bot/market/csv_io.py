from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.exchange.instruments import to_decimal
from trading_bot.market.candles import Candle, interval_to_ms, to_bybit_interval


def candles_from_csv(path: str | Path, symbol: str, timeframe: str) -> list[Candle]:
    interval = to_bybit_interval(timeframe)
    duration = interval_to_ms(interval)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows: list[Candle] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"empty csv: {path}")
        fields = {name.lower(): name for name in reader.fieldnames}
        for raw in reader:
            start_ms = _start_ms(raw, fields)
            rows.append(
                Candle(
                    symbol=symbol,
                    interval=interval,
                    start_time=datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
                    open=to_decimal(_col(raw, fields, "open")),
                    high=to_decimal(_col(raw, fields, "high")),
                    low=to_decimal(_col(raw, fields, "low")),
                    close=to_decimal(_col(raw, fields, "close")),
                    volume=to_decimal(_col(raw, fields, "volume") or "0"),
                    turnover=to_decimal(_col(raw, fields, "turnover") or "0"),
                    confirmed=start_ms + duration <= now_ms,
                )
            )
    rows.sort(key=lambda c: c.start_ms)
    return rows


def _col(raw: dict[str, str], fields: dict[str, str], name: str) -> str:
    key = fields.get(name)
    if key is None:
        return ""
    return raw.get(key, "")


def _start_ms(raw: dict[str, str], fields: dict[str, str]) -> int:
    for candidate in ("start_ms", "timestamp", "time", "open_time"):
        key = fields.get(candidate)
        if not key:
            continue
        value = raw[key].strip()
        if not value:
            continue
        if value.isdigit():
            number = int(value)
            return number if number > 10_000_000_000 else number * 1000
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    raise ValueError("csv needs start_ms or timestamp column")
