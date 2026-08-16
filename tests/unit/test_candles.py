from __future__ import annotations

from datetime import datetime, timezone

from trading_bot.market.candles import parse_kline_payload, parse_kline_row, to_bybit_interval


def test_timeframe_maps_to_bybit_interval() -> None:
    assert to_bybit_interval("15m") == "15"
    assert to_bybit_interval("1h") == "60"
    assert to_bybit_interval("1d") == "D"


def test_unclosed_candle_is_not_confirmed() -> None:
    start_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    row = [str(start_ms), "100", "110", "90", "105", "1", "100"]
    still_open = parse_kline_row("BTCUSDT", "15", row, now_ms=start_ms + 15 * 60 * 1000 - 1)
    closed = parse_kline_row("BTCUSDT", "15", row, now_ms=start_ms + 15 * 60 * 1000)
    assert still_open.confirmed is False
    assert closed.confirmed is True


def test_bybit_newest_first_is_reversed_and_unclosed_dropped() -> None:
    start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    step = 15 * 60 * 1000
    newer = [str(start + step), "2", "2", "2", "2", "1", "1"]
    older = [str(start), "1", "1", "1", "1", "1", "1"]
    payload = {"result": {"symbol": "BTCUSDT", "list": [newer, older]}}
    now_ms = start + 2 * step
    candles = parse_kline_payload(payload, interval="15", now_ms=now_ms, include_unclosed=False)
    assert [c.start_ms for c in candles] == [start, start + step]
    # If "now" is inside the second bar, only the first bar is confirmed.
    mid_second = start + step + 1
    confirmed_only = parse_kline_payload(payload, interval="15", now_ms=mid_second, include_unclosed=False)
    assert len(confirmed_only) == 1
    assert confirmed_only[0].start_ms == start
