from __future__ import annotations


class TradingBotError(Exception):
    """Base error for the trading bot."""


class ConfigError(TradingBotError):
    pass


class BybitAPIError(TradingBotError):
    def __init__(self, message: str, *, ret_code: int | None = None, status_code: int | None = None):
        super().__init__(message)
        self.ret_code = ret_code
        self.status_code = status_code


class RateLimitError(BybitAPIError):
    pass


class AuthenticationError(BybitAPIError):
    pass


class InsufficientBalanceError(BybitAPIError):
    pass


class InvalidOrderError(BybitAPIError):
    pass


class ConnectionLostError(TradingBotError):
    pass


class StaleMarketDataError(TradingBotError):
    pass


class KillSwitchActiveError(TradingBotError):
    pass


class StopLossMissingError(TradingBotError):
    """Protective stop was not confirmed on the exchange after opening a position."""


class WithdrawPermissionError(TradingBotError):
    """API key has Withdraw permission, which this bot refuses to use."""


class GeoRestrictedError(BybitAPIError):
    """Bybit CloudFront blocked this IP/country. Not retryable."""
