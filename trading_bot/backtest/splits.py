from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.config.models import BacktestFallbackInstrument
from trading_bot.exchange.instruments import Instrument


def fallback_instrument(symbol: str, spec: BacktestFallbackInstrument, *, category: str = "linear") -> Instrument:
    """Config-driven constraints when the live instruments-info cache is empty."""
    payload = {
        "symbol": symbol,
        "status": "Trading",
        "baseCoin": symbol.replace("USDT", ""),
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "contractType": "LinearPerpetual",
        "priceFilter": {"tickSize": str(spec.tick_size), "minPrice": str(spec.tick_size), "maxPrice": "10000000"},
        "lotSizeFilter": {
            "qtyStep": str(spec.qty_step),
            "minOrderQty": str(spec.min_order_qty),
            "maxOrderQty": str(spec.max_order_qty),
            "maxMktOrderQty": str(spec.max_order_qty),
            "minNotionalValue": str(spec.min_notional),
        },
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100", "leverageStep": "0.01"},
        "fundingInterval": 480,
    }
    return Instrument(
        category=category,
        symbol=symbol,
        status="Trading",
        base_coin=payload["baseCoin"],
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        tick_size=spec.tick_size,
        qty_step=spec.qty_step,
        min_order_qty=spec.min_order_qty,
        max_order_qty=spec.max_order_qty,
        max_mkt_order_qty=spec.max_order_qty,
        min_notional=spec.min_notional,
        min_leverage=Decimal("1"),
        max_leverage=Decimal("100"),
        leverage_step=Decimal("0.01"),
        min_price=spec.tick_size,
        max_price=Decimal("10000000"),
        funding_interval_min=480,
        raw=payload,
    )


def funding_events_in_bar(start_ms: int, interval_ms: int, funding_interval_hours: int) -> int:
    step = funding_interval_hours * 3_600_000
    if step <= 0 or interval_ms <= 0:
        return 0
    end = start_ms + interval_ms
    first = ((start_ms + step - 1) // step) * step
    if first >= end:
        return 0
    return (end - 1 - first) // step + 1


def bar_close_time(start: datetime, interval_ms: int) -> datetime:
    return datetime.fromtimestamp(start.timestamp() + interval_ms / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class SplitRange:
    name: str
    start: int
    end: int


def split_ranges(n: int, development: Decimal, validation: Decimal, out_of_sample: Decimal) -> list[SplitRange]:
    if n <= 0:
        return []
    a = int(n * float(development))
    b = a + int(n * float(validation))
    return [
        SplitRange("development", 0, max(a, 0)),
        SplitRange("validation", a, max(b, a)),
        SplitRange("out_of_sample", b, n),
    ]


def walk_forward_windows(
    n: int,
    *,
    train_fraction: Decimal,
    test_fraction: Decimal,
    step_fraction: Decimal,
) -> list[tuple[int, int, int]]:
    """(train_start, test_start, test_end) with train immediately before test."""
    train = max(1, int(n * float(train_fraction)))
    test = max(1, int(n * float(test_fraction)))
    step = max(1, int(n * float(step_fraction)))
    windows: list[tuple[int, int, int]] = []
    start = 0
    while start + train + test <= n:
        test_start = start + train
        test_end = test_start + test
        windows.append((start, test_start, test_end))
        start += step
    return windows
