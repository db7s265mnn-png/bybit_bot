from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from websocket import WebSocketApp, WebSocketConnectionClosedException

from trading_bot.config.models import AppConfig, ProductCategory
from trading_bot.core.exceptions import AuthenticationError, ConnectionLostError
from trading_bot.core.redaction import redact_text
from trading_bot.core.retry import sleep_backoff
from trading_bot.core.types import ConnectionState
from trading_bot.monitoring.logger import get_logger

logger = get_logger("trading_bot.websocket")

OnMessage = Callable[[dict[str, Any]], None]
OnState = Callable[[ConnectionState, str], None]


class StreamKind(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


def public_ws_url(testnet: bool, category: ProductCategory, domain: str = "bybit") -> str:
    host = f"stream-testnet.{domain}.com" if testnet else f"stream.{domain}.com"
    return f"wss://{host}/v5/public/{category.value}"


def private_ws_url(testnet: bool, domain: str = "bybit") -> str:
    host = f"stream-testnet.{domain}.com" if testnet else f"stream.{domain}.com"
    return f"wss://{host}/v5/private"


def kline_topic(interval: str, symbol: str) -> str:
    return f"kline.{interval}.{symbol}"


def ticker_topic(symbol: str) -> str:
    return f"tickers.{symbol}"


def orderbook_topic(symbol: str, depth: int = 1) -> str:
    return f"orderbook.{depth}.{symbol}"


class BybitWebSocket:
    """Bybit V5 WebSocket with heartbeat, exponential backoff, and trading-pause on disconnect.

    While the socket is not CONNECTED, `trading_paused` is True. Callers must not
    send orders until the state returns to CONNECTED and local state is resynced.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        kind: StreamKind = StreamKind.PUBLIC,
        on_message: OnMessage | None = None,
        on_state: OnState | None = None,
    ) -> None:
        self._config = config
        self._kind = kind
        self._on_message = on_message
        self._on_state = on_state
        self._topics: list[str] = []
        self._state = ConnectionState.DISCONNECTED
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: WebSocketApp | None = None
        self._last_message_at: float | None = None
        self._last_pong_at: float | None = None
        self._attempt = 0
        self._url = (
            private_ws_url(config.exchange.testnet, config.exchange.domain)
            if kind is StreamKind.PRIVATE
            else public_ws_url(config.exchange.testnet, config.exchange.category, config.exchange.domain)
        )

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def trading_paused(self) -> bool:
        return self.state is not ConnectionState.CONNECTED

    @property
    def last_message_at(self) -> datetime | None:
        with self._lock:
            if self._last_message_at is None:
                return None
            return datetime.fromtimestamp(self._last_message_at, tz=timezone.utc)

    def seconds_since_last_message(self) -> float | None:
        with self._lock:
            if self._last_message_at is None:
                return None
            return time.monotonic() - self._last_message_at

    def subscribe(self, topics: list[str]) -> None:
        unique = []
        for topic in topics:
            if topic not in unique:
                unique.append(topic)
        with self._lock:
            self._topics = unique
            ws = self._ws
            connected = self._state is ConnectionState.CONNECTED
        if connected and ws is not None:
            self._send_subscribe(ws, unique)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="bybit-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._set_state(ConnectionState.STOPPED, "stopped")

    def wait_connected(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state is ConnectionState.CONNECTED:
                return
            if self.state is ConnectionState.FAILED:
                raise ConnectionLostError("websocket failed to connect")
            time.sleep(0.05)
        raise ConnectionLostError(f"websocket not connected within {timeout}s (state={self.state.value})")

    def _run_loop(self) -> None:
        ws_cfg = self._config.websocket
        while not self._stop.is_set():
            if self._attempt >= ws_cfg.max_reconnect_attempts:
                self._set_state(
                    ConnectionState.FAILED,
                    f"reconnect attempts exhausted ({ws_cfg.max_reconnect_attempts})",
                )
                logger.critical("websocket_failed", url=self._url, attempts=self._attempt)
                return
            if self._attempt == 0:
                self._set_state(ConnectionState.CONNECTING, "connecting")
            else:
                delay = sleep_backoff(
                    self._attempt - 1,
                    ws_cfg.reconnect_base_delay_sec,
                    ws_cfg.reconnect_max_delay_sec,
                )
                if self._stop.is_set():
                    return
                self._set_state(ConnectionState.RECONNECTING, f"reconnect delay {delay:.2f}s")
                logger.warning(
                    "websocket_reconnect",
                    attempt=self._attempt,
                    delay_sec=round(delay, 3),
                    url=self._url,
                )
            self._attempt += 1
            connected = self._open_once()
            if self._stop.is_set():
                return
            if connected:
                self._attempt = 0

    def _open_once(self) -> bool:
        opened = threading.Event()
        saw_error = threading.Event()

        def on_open(ws: WebSocketApp) -> None:
            logger.info("websocket_open", url=self._url, kind=self._kind.value)
            if self._kind is StreamKind.PRIVATE:
                try:
                    self._authenticate(ws)
                except Exception as exc:  # noqa: BLE001
                    logger.error("websocket_auth_failed", error=redact_text(repr(exc)))
                    saw_error.set()
                    ws.close()
                    return
            self._send_subscribe(ws, list(self._topics))
            self._set_state(ConnectionState.CONNECTED, "connected")
            opened.set()

        def on_message(_ws: WebSocketApp, message: str) -> None:
            self._mark_message()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("websocket_bad_json")
                return
            if self._is_pong(payload):
                with self._lock:
                    self._last_pong_at = time.monotonic()
                return
            if payload.get("op") == "auth":
                if not payload.get("success"):
                    logger.error("websocket_auth_rejected", ret_msg=payload.get("ret_msg"))
                    saw_error.set()
                return
            if payload.get("op") == "subscribe":
                if payload.get("success") is False:
                    logger.error("websocket_subscribe_failed", ret_msg=payload.get("ret_msg"))
                return
            if self._on_message:
                self._on_message(payload)

        def on_error(_ws: WebSocketApp, error: Any) -> None:
            try:
                logger.error("websocket_error", error=redact_text(repr(error)))
            except Exception:  # noqa: BLE001
                pass
            saw_error.set()

        def on_close(_ws: WebSocketApp, status: Any, msg: Any) -> None:
            try:
                logger.warning("websocket_closed", status=status, message=msg)
            except Exception:  # noqa: BLE001
                pass
            if self.state not in {ConnectionState.STOPPED, ConnectionState.FAILED}:
                self._set_state(ConnectionState.PAUSED, "socket closed; trading paused")

        self._ws = WebSocketApp(
            self._url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        runner = threading.Thread(
            target=lambda: self._ws.run_forever(ping_interval=None, ping_timeout=None)
            if self._ws
            else None,
            name="bybit-ws-socket",
            daemon=True,
        )
        runner.start()
        heartbeat = threading.Thread(target=self._heartbeat_loop, name="bybit-ws-heartbeat", daemon=True)
        heartbeat.start()
        runner.join()
        return opened.is_set() and not saw_error.is_set() and not self._stop.is_set()

    def _heartbeat_loop(self) -> None:
        interval = self._config.websocket.ping_interval_sec
        stale = self._config.websocket.stale_after_sec
        while not self._stop.is_set() and self._ws is not None:
            time.sleep(interval)
            ws = self._ws
            if ws is None or self._stop.is_set():
                return
            if self.state is not ConnectionState.CONNECTED:
                return
            try:
                ws.send(json.dumps({"op": "ping"}))
            except WebSocketConnectionClosedException:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("websocket_ping_failed", error=redact_text(repr(exc)))
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            age = self.seconds_since_last_message()
            if age is not None and age > stale:
                logger.error("websocket_stale", age_sec=round(age, 3), stale_after_sec=stale)
                self._set_state(ConnectionState.PAUSED, "stale market data; trading paused")
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
                return

    def _authenticate(self, ws: WebSocketApp) -> None:
        key = self._config.secrets.bybit_api_key
        secret = self._config.secrets.bybit_api_secret
        if not key or not secret:
            raise AuthenticationError("private websocket requires BYBIT_API_KEY and BYBIT_API_SECRET")
        expires = int((time.time() + 1) * 1000)
        signature = hmac.new(
            secret.encode("utf-8"),
            f"GET/realtime{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        ws.send(json.dumps({"op": "auth", "args": [key, expires, signature]}))

    def _send_subscribe(self, ws: WebSocketApp, topics: list[str]) -> None:
        if not topics:
            return
        ws.send(json.dumps({"op": "subscribe", "args": topics}))
        logger.info("websocket_subscribe", topics=topics)

    def _mark_message(self) -> None:
        with self._lock:
            self._last_message_at = time.monotonic()

    def _set_state(self, state: ConnectionState, reason: str) -> None:
        with self._lock:
            if self._state is state and state is not ConnectionState.RECONNECTING:
                return
            self._state = state
        logger.info(
            "websocket_state",
            state=state.value,
            reason=reason,
            trading_paused=state is not ConnectionState.CONNECTED,
        )
        if self._on_state:
            self._on_state(state, reason)

    @staticmethod
    def _is_pong(payload: dict[str, Any]) -> bool:
        return payload.get("op") in {"pong", "ping"} and (
            payload.get("ret_msg") == "pong" or payload.get("op") == "pong" or payload.get("success") is True
        )
