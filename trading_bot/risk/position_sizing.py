from __future__ import annotations

from decimal import Decimal

from trading_bot.core.types import SignalType
from trading_bot.execution.slippage import taker_fee


def loss_per_unit(
    signal_type: SignalType,
    entry: Decimal,
    stop: Decimal,
    *,
    taker: Decimal,
    slippage: Decimal,
) -> Decimal:
    """Loss if price hits the stop, per 1 contract/coin, including fees and assumed slippage."""
    move = (entry - stop) if signal_type is SignalType.LONG else (stop - entry)
    if move <= 0:
        return Decimal("0")
    fees = taker_fee(entry, taker) + taker_fee(stop, taker)
    return move + fees + entry * slippage


def size_from_risk(
    equity: Decimal,
    risk_per_trade: Decimal,
    loss_per_unit_value: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return (quantity, risk_amount). Quantity is unrounded."""
    risk_amount = equity * risk_per_trade
    if loss_per_unit_value <= 0:
        return Decimal("0"), risk_amount
    return risk_amount / loss_per_unit_value, risk_amount
