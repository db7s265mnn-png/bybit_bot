from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_bot.config.models import StrategyParams, TakeProfitType
from trading_bot.core.types import SignalType
from trading_bot.market.candles import Candle
from trading_bot.strategy.base import Signal, Strategy, hold_signal, register
from trading_bot.strategy.indicators import atr, ema


@register("ema_crossover")
class EmaCrossoverStrategy(Strategy):
    """Baseline EMA 20/50 cross with ATR stop. Infrastructure test only — not a profitability claim."""

    name = "ema_crossover"

    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = params or StrategyParams()
        if self.params.fast_ema >= self.params.slow_ema:
            raise ValueError("fast_ema must be < slow_ema")

    def required_history(self) -> int:
        return max(self.params.slow_ema, self.params.atr_period) + 2

    def generate_signal(self, history: Sequence[Candle]) -> Signal:
        if not history:
            raise ValueError("history must not be empty")
        last = history[-1]
        extra: dict = {}
        if len(history) < self.required_history():
            return hold_signal(last.symbol, last.start_time, extra)
        closes = [c.close for c in history]
        fast = ema(closes, self.params.fast_ema)
        slow = ema(closes, self.params.slow_ema)
        atr_series = atr(history, self.params.atr_period)
        fast_now, slow_now = fast[-1], slow[-1]
        fast_prev, slow_prev = fast[-2], slow[-2]
        atr_now = atr_series[-1]
        extra = {
            "fast_ema": None if fast_now is None else str(fast_now),
            "slow_ema": None if slow_now is None else str(slow_now),
            "atr": None if atr_now is None else str(atr_now),
        }
        if None in (fast_now, slow_now, fast_prev, slow_prev, atr_now):
            return hold_signal(last.symbol, last.start_time, extra)
        assert fast_now is not None and slow_now is not None
        assert fast_prev is not None and slow_prev is not None
        assert atr_now is not None
        golden = fast_prev <= slow_prev and fast_now > slow_now
        death = fast_prev >= slow_prev and fast_now < slow_now
        if not golden and not death:
            return hold_signal(last.symbol, last.start_time, extra)
        signal_type = SignalType.LONG if golden else SignalType.SHORT
        stop = self._stop_loss(last.close, atr_now, signal_type)
        take = self._take_profit(last.close, stop, atr_now, signal_type)
        if stop is None or take is None:
            return hold_signal(last.symbol, last.start_time, extra)
        confidence = min(Decimal("1"), abs(fast_now - slow_now) / last.close * Decimal("50"))
        return Signal(
            type=signal_type,
            symbol=last.symbol,
            timestamp=last.start_time,
            confidence=confidence,
            stop_loss=stop,
            take_profit=take,
            recommended_size=None,
            extra=extra,
        )

    def _stop_loss(self, close: Decimal, atr_now: Decimal, signal_type: SignalType) -> Decimal | None:
        distance = atr_now * self.params.atr_sl_multiplier
        if distance <= 0:
            return None
        if signal_type is SignalType.LONG:
            stop = close - distance
            return stop if stop > 0 else None
        return close + distance

    def _take_profit(
        self,
        close: Decimal,
        stop: Decimal,
        atr_now: Decimal,
        signal_type: SignalType,
    ) -> Decimal | None:
        tp = self.params.take_profit
        direction = Decimal("1") if signal_type is SignalType.LONG else Decimal("-1")
        if tp.type is TakeProfitType.RISK_REWARD:
            risk = abs(close - stop)
            if risk <= 0:
                return None
            return close + direction * risk * tp.risk_reward
        if tp.type is TakeProfitType.ATR:
            return close + direction * atr_now * tp.atr_multiplier
        return close * (Decimal("1") + direction * tp.fixed_pct)
