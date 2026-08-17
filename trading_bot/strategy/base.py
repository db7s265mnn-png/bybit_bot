from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.core.exceptions import ConfigError
from trading_bot.core.types import SignalType
from trading_bot.market.candles import Candle

StrategyFactory = Callable[..., "Strategy"]
_REGISTRY: dict[str, StrategyFactory] = {}


def register(name: str) -> Callable[[StrategyFactory], StrategyFactory]:
    """Decorator so a new strategy file can register itself without touching other packages."""

    def decorator(factory: StrategyFactory) -> StrategyFactory:
        _REGISTRY[name] = factory
        return factory

    return decorator


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


@dataclass(frozen=True)
class Signal:
    type: SignalType
    symbol: str
    timestamp: datetime
    confidence: Decimal = Decimal("0")
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    recommended_size: Decimal | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def is_entry(self) -> bool:
        return self.type in {SignalType.LONG, SignalType.SHORT}


class Strategy(ABC):
    """Strategies emit signals only. They never send orders."""

    name: str = "base"

    @abstractmethod
    def required_history(self) -> int:
        """Minimum closed candles before the first signal may be non-HOLD."""

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> None:
        """Optional hook after a candle is closed. Default: no internal state."""

    @abstractmethod
    def generate_signal(self, history: Sequence[Candle]) -> Signal:
        """`history[-1]` is the candle that just closed. No future bars are included."""


def hold_signal(symbol: str, timestamp: datetime, extra: dict[str, Any] | None = None) -> Signal:
    return Signal(
        type=SignalType.HOLD,
        symbol=symbol,
        timestamp=timestamp,
        confidence=Decimal("0"),
        extra=extra or {},
    )


def create_strategy(name: str, **kwargs: Any) -> Strategy:
    if name not in _REGISTRY:
        raise ConfigError(
            f"unknown strategy {name!r}. Available: {available_strategies()}. "
            "Add a module under trading_bot/strategy and decorate it with @register."
        )
    return _REGISTRY[name](**kwargs)
