from __future__ import annotations

from decimal import Decimal

from trading_bot.core.types import Side, SignalType


def fill_price(
    reference: Decimal,
    signal_type: SignalType | Side | str,
    *,
    slippage: Decimal,
    spread: Decimal = Decimal("0"),
    is_entry: bool = True,
) -> Decimal:
    """Adverse fill: buy higher, sell lower. `spread` is the full bid-ask spread fraction."""
    side = _as_buy(signal_type, is_entry=is_entry)
    half_spread = spread / Decimal("2")
    bump = slippage + half_spread
    if side:
        return reference * (Decimal("1") + bump)
    return reference * (Decimal("1") - bump)


def _as_buy(signal_type: SignalType | Side | str, *, is_entry: bool) -> bool:
    raw = signal_type.value if hasattr(signal_type, "value") else str(signal_type)
    lowered = raw.lower()
    is_long = lowered in {"long", "buy"}
    is_short = lowered in {"short", "sell"}
    if is_entry:
        return is_long
    return is_short


def taker_fee(notional: Decimal, rate: Decimal) -> Decimal:
    return abs(notional) * rate
