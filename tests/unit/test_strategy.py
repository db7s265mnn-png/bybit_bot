from __future__ import annotations

from decimal import Decimal

from trading_bot.config.models import StrategyParams, TakeProfitConfig, TakeProfitType
from trading_bot.core.types import SignalType
from trading_bot.strategy.ema_strategy import EmaCrossoverStrategy
from trading_bot.strategy.indicators import ema
from tests.helpers import make_candles


def test_ema_golden_cross_emits_long() -> None:
    # Long stretch of a low close, then a jump so the fast EMA crosses above the slow EMA.
    closes = [100] * 60 + [130] * 25
    candles = make_candles(closes)
    strategy = EmaCrossoverStrategy(
        StrategyParams(
            fast_ema=5,
            slow_ema=20,
            atr_period=5,
            atr_sl_multiplier=Decimal("2"),
            take_profit=TakeProfitConfig(type=TakeProfitType.RISK_REWARD, risk_reward=Decimal("2")),
        )
    )
    longs = 0
    shorts = 0
    for i in range(strategy.required_history(), len(candles) + 1):
        signal = strategy.generate_signal(candles[:i])
        if signal.type is SignalType.LONG:
            longs += 1
            assert signal.stop_loss is not None
            assert signal.take_profit is not None
            assert signal.stop_loss < candles[i - 1].close
            assert signal.take_profit > candles[i - 1].close
        elif signal.type is SignalType.SHORT:
            shorts += 1
    assert longs >= 1
    assert shorts == 0


def test_ema_death_cross_emits_short() -> None:
    closes = [130] * 60 + [100] * 25
    candles = make_candles(closes)
    strategy = EmaCrossoverStrategy(
        StrategyParams(fast_ema=5, slow_ema=20, atr_period=5, atr_sl_multiplier=Decimal("2"))
    )
    shorts = 0
    for i in range(strategy.required_history(), len(candles) + 1):
        if strategy.generate_signal(candles[:i]).type is SignalType.SHORT:
            shorts += 1
    assert shorts >= 1


def test_ema_series_rises_toward_price() -> None:
    values = [Decimal("1")] * 10 + [Decimal("10")]
    series = ema(values, 5)
    assert series[-1] is not None
    assert series[-1] > series[-2]
