"""Strategy layer. Import concrete modules so @register side effects run."""

from trading_bot.strategy import ema_strategy as _ema_strategy  # noqa: F401
from trading_bot.strategy.base import Signal, Strategy, create_strategy, register

__all__ = ["Signal", "Strategy", "create_strategy", "register"]
