from __future__ import annotations

from enum import Enum

from trading_bot.config.models import TradingMode

__all__ = ["ConnectionState", "Side", "SignalType", "TradingMode"]


class Side(str, Enum):
    BUY = "Buy"
    SELL = "Sell"


class SignalType(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"
    HOLD = "HOLD"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    PAUSED = "paused"
    FAILED = "failed"
    STOPPED = "stopped"
