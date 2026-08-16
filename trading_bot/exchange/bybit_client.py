from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP
from tenacity import RetryCallState

from trading_bot.config.models import AppConfig, ProductCategory
from trading_bot.core.exceptions import (
    AuthenticationError,
    BybitAPIError,
    GeoRestrictedError,
    InsufficientBalanceError,
    InvalidOrderError,
    RateLimitError,
    WithdrawPermissionError,
)
from trading_bot.core.redaction import redact_text
from trading_bot.core.retry import retrying
from trading_bot.core.time_sync import TimeSync
from trading_bot.exchange.instruments import Instrument, parse_instrument
from trading_bot.exchange.rate_limiter import TokenBucketRateLimiter
from trading_bot.monitoring.logger import get_logger

logger = get_logger("trading_bot.exchange")

_AUTH_CODES = {10003, 10004, 10005, 10007, 10010, 33004}
_RATE_CODES = {10006, 10018, 10429, 429}
_BALANCE_CODES = {110007, 110004}
_ORDER_CODES = {10001, 110001, 110003, 110004, 110094}


def _looks_geo_restricted(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("from your country", "cloudfront", "from the usa", "from the united states")
    )


def _map_pybit_error(exc: Exception) -> Exception:
    message = redact_text(str(exc))
    status = getattr(exc, "status_code", None)
    if isinstance(exc, InvalidRequestError):
        code = status
        if code in _AUTH_CODES:
            return AuthenticationError(message, ret_code=code, status_code=code)
        if code in _RATE_CODES:
            return RateLimitError(message, ret_code=code, status_code=code)
        if code in _BALANCE_CODES:
            return InsufficientBalanceError(message, ret_code=code, status_code=code)
        if code in _ORDER_CODES:
            return InvalidOrderError(message, ret_code=code, status_code=code)
        return BybitAPIError(message, ret_code=code, status_code=code)
    if isinstance(exc, FailedRequestError):
        code = status
        if _looks_geo_restricted(message):
            return GeoRestrictedError(
                "Bybit blocked this IP/country (HTTP 403). "
                "This is not a rate-limit retry. Use a VPS/IP that Bybit allows. "
                f"Original: {message}",
                ret_code=code,
                status_code=code,
            )
        if code in {403, 429}:
            return RateLimitError(message, ret_code=code, status_code=code)
        if code in {401}:
            return AuthenticationError(message, ret_code=code, status_code=code)
        return BybitAPIError(message, ret_code=code, status_code=code)
    return exc


class BybitRESTClient:
    """Official Bybit V5 REST via pybit. Secrets never appear in logs."""

    def __init__(self, config: AppConfig, *, http: HTTP | None = None) -> None:
        self._config = config
        self._limiter = TokenBucketRateLimiter(config.exchange.max_requests_per_second)
        self._http = http or HTTP(
            testnet=config.exchange.testnet,
            domain=config.exchange.domain,
            api_key=config.secrets.bybit_api_key or None,
            api_secret=config.secrets.bybit_api_secret or None,
            timeout=config.exchange.rest_timeout_sec,
            recv_window=config.exchange.recv_window_ms,
            max_retries=1,
            force_retry=False,
            log_requests=False,
        )
        self._instrument_cache: dict[str, Instrument] = {}
        extra = [config.secrets.bybit_api_secret, config.secrets.bybit_api_key]

        def _before_sleep(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            logger.warning(
                "bybit_rest_retry",
                attempt=state.attempt_number,
                error=redact_text(repr(exc), extra_secrets=extra) if exc else None,
            )

        self._call = retrying(
            max_attempts=config.exchange.max_retries + 1,
            base_delay=config.exchange.retry_base_delay_sec,
            max_delay=config.exchange.retry_max_delay_sec,
            before_sleep=_before_sleep,
        )(self._invoke)

    @property
    def category(self) -> ProductCategory:
        return self._config.exchange.category

    @property
    def testnet(self) -> bool:
        return self._config.exchange.testnet

    def _invoke(self, method: str, **kwargs: Any) -> dict[str, Any]:
        self._limiter.acquire()
        fn: Callable[..., Any] = getattr(self._http, method)
        try:
            response = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 — mapped to domain errors
            raise _map_pybit_error(exc) from exc
        if not isinstance(response, dict):
            raise BybitAPIError("unexpected Bybit response type")
        ret_code = int(response.get("retCode") or 0)
        if ret_code != 0:
            raise BybitAPIError(
                redact_text(str(response.get("retMsg") or "bybit error")),
                ret_code=ret_code,
            )
        return response

    def get_server_time(self) -> TimeSync:
        local_before = int(time.time() * 1000)
        payload = self._call("get_server_time")
        local_after = int(time.time() * 1000)
        result = payload.get("result") or {}
        server_ms = int(result.get("timeNano", 0)) // 1_000_000 or int(
            result.get("timeSecond", 0)
        ) * 1000
        if not server_ms:
            server_ms = int(payload.get("time") or 0)
        local_mid = (local_before + local_after) // 2
        sync = TimeSync(
            offset_ms=server_ms - local_mid,
            server_time_ms=server_ms,
            local_time_ms=local_mid,
        )
        if sync.skew_too_large:
            logger.warning("clock_skew", offset_ms=sync.offset_ms)
        return sync

    def get_instruments(self, symbol: str | None = None, *, use_cache: bool = True) -> list[Instrument]:
        symbols = [symbol] if symbol else list(self._config.exchange.symbols)
        found: list[Instrument] = []
        for item_symbol in symbols:
            if use_cache and item_symbol in self._instrument_cache:
                found.append(self._instrument_cache[item_symbol])
                continue
            payload = self._call(
                "get_instruments_info",
                category=self.category.value,
                symbol=item_symbol,
            )
            rows = (payload.get("result") or {}).get("list") or []
            if not rows:
                raise BybitAPIError(f"instrument not found: {item_symbol}")
            instrument = parse_instrument(self.category.value, rows[0])
            self._instrument_cache[item_symbol] = instrument
            found.append(instrument)
        return found

    def get_instrument(self, symbol: str, *, use_cache: bool = True) -> Instrument:
        return self.get_instruments(symbol, use_cache=use_cache)[0]

    def get_kline(
        self,
        symbol: str,
        interval: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._call(
            "get_kline",
            category=self.category.value,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            limit=min(max(limit, 1), 1000),
        )

    def get_tickers(self, symbol: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"category": self.category.value}
        if symbol:
            kwargs["symbol"] = symbol
        return self._call("get_tickers", **kwargs)

    def get_orderbook(self, symbol: str, limit: int = 25) -> dict[str, Any]:
        return self._call(
            "get_orderbook",
            category=self.category.value,
            symbol=symbol,
            limit=limit,
        )

    def get_funding_history(self, symbol: str, limit: int = 50) -> dict[str, Any]:
        return self._call(
            "get_funding_rate_history",
            category=self.category.value,
            symbol=symbol,
            limit=limit,
        )

    def get_wallet_balance(self, coin: str | None = None) -> dict[str, Any]:
        self._require_keys()
        kwargs: dict[str, Any] = {"accountType": self._config.exchange.account_type}
        if coin:
            kwargs["coin"] = coin
        return self._call("get_wallet_balance", **kwargs)

    def get_positions(self, symbol: str | None = None) -> dict[str, Any]:
        self._require_keys()
        kwargs: dict[str, Any] = {"category": self.category.value}
        if symbol:
            kwargs["symbol"] = symbol
        else:
            kwargs["settleCoin"] = self._config.exchange.settle_coin
        return self._call("get_positions", **kwargs)

    def get_open_orders(self, symbol: str | None = None) -> dict[str, Any]:
        self._require_keys()
        kwargs: dict[str, Any] = {"category": self.category.value, "settleCoin": self._config.exchange.settle_coin}
        if symbol:
            kwargs["symbol"] = symbol
            kwargs.pop("settleCoin", None)
        return self._call("get_open_orders", **kwargs)

    def get_executions(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        self._require_keys()
        kwargs: dict[str, Any] = {"category": self.category.value, "limit": limit}
        if symbol:
            kwargs["symbol"] = symbol
        return self._call("get_executions", **kwargs)

    def get_fee_rates(self, symbol: str | None = None) -> dict[str, Any]:
        self._require_keys()
        kwargs: dict[str, Any] = {"category": self.category.value}
        if symbol:
            kwargs["symbol"] = symbol
        return self._call("get_fee_rates", **kwargs)

    def get_api_key_information(self) -> dict[str, Any]:
        self._require_keys()
        return self._call("get_api_key_information")

    def assert_safe_api_key(self) -> dict[str, Any]:
        """Refuse to run if the key can withdraw. Logs permissions without the secret."""
        payload = self.get_api_key_information()
        result = payload.get("result") or {}
        permissions = result.get("permissions") or {}
        wallet = permissions.get("Wallet") or []
        if any(str(item).lower() == "withdraw" for item in wallet):
            raise WithdrawPermissionError(
                "API key has Withdraw permission. Create a dedicated bot key without withdrawal."
            )
        ips = result.get("ips") or []
        logger.info(
            "api_key_checked",
            read_only=result.get("readOnly"),
            uta=result.get("uta"),
            ip_bound=bool(ips) and ips != ["*"],
            wallet_permissions=wallet,
            contract_trade=permissions.get("ContractTrade"),
            derivatives=permissions.get("Derivatives"),
            note=result.get("note"),
        )
        if not ips or ips == ["*"]:
            logger.warning("api_key_has_no_ip_whitelist")
        return result

    def _require_keys(self) -> None:
        if not self._config.secrets.has_bybit_keys():
            raise AuthenticationError(
                "BYBIT_API_KEY and BYBIT_API_SECRET are required for private endpoints"
            )

    def host_label(self) -> str:
        env = "testnet" if self.testnet else "mainnet"
        return f"bybit-{env}-{self.category.value}"

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)
