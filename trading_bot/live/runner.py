from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from trading_bot.config.models import AppConfig
from trading_bot.core.exceptions import ConnectionLostError, GeoRestrictedError, TradingBotError
from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.exchange.websocket import BybitWebSocket, StreamKind, kline_topic
from trading_bot.live.engine import TESTNET_DISCLAIMER, LiveEngine
from trading_bot.market.candles import Candle, to_bybit_interval
from trading_bot.market.market_data import MarketDataService
from trading_bot.monitoring.logger import get_logger
from trading_bot.paper.runner import split_history_and_gap

logger = get_logger("trading_bot.live.runner")


def apply_leverage(
    config: AppConfig,
    client: BybitRESTClient,
    symbols: list[str],
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        logger.info("live_dry_run_skip_leverage")
        return
    lev = format(config.trading.leverage, "f")
    for symbol in symbols:
        try:
            client.set_leverage(symbol=symbol, buy_leverage=lev, sell_leverage=lev)
        except TradingBotError as exc:
            logger.error("live_set_leverage_failed", symbol=symbol, error=str(exc))


def run_testnet_loop(
    config: AppConfig,
    engine: LiveEngine,
    *,
    market: MarketDataService,
    symbols: list[str],
    seconds: float | None,
    warmup: int,
    client: BybitRESTClient | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.time,
    websocket_factory: Callable[..., BybitWebSocket] | None = None,
    private_websocket_factory: Callable[..., BybitWebSocket] | None = None,
    enable_private_ws: bool = True,
) -> dict[str, Any]:
    """Public kline stream + REST warmup. Orders only on live confirmed klines, via OrderManager."""
    interval = to_bybit_interval(config.trading.timeframe)
    warmup_bars: list[Candle] = []
    for symbol in symbols:
        try:
            warmup_bars.extend(
                market.candles(symbol, config.trading.timeframe, limit=warmup, include_unclosed=False)
            )
        except GeoRestrictedError:
            raise
        except TradingBotError as exc:
            logger.warning("live_warmup_failed", symbol=symbol, error=str(exc))
    history, gap = split_history_and_gap(warmup_bars, engine.last_ms)
    engine.load_history(history)
    engine.seed_if_fresh()

    factory = websocket_factory or BybitWebSocket
    ws = factory(
        config,
        kind=StreamKind.PUBLIC,
        on_message=engine.handle_ws_message,
        on_state=engine.on_connection_state,
    )
    ws.subscribe([kline_topic(interval, symbol) for symbol in symbols])
    ws.start()
    private_ws: BybitWebSocket | None = None
    try:
        ws.wait_connected(timeout=20)
        engine.set_paused(False, reason="public websocket connected")
        if enable_private_ws:
            try:
                pfactory = private_websocket_factory or BybitWebSocket
                private_ws = pfactory(
                    config,
                    kind=StreamKind.PRIVATE,
                    on_message=engine.handle_private_message,
                    on_state=engine.on_private_state,
                )
                private_ws.subscribe(["order", "position"])
                private_ws.start()
                private_ws.wait_connected(timeout=15)
            except Exception as exc:  # noqa: BLE001 — private WS is optional; REST confirms fills
                logger.warning(
                    "live_private_ws_unavailable",
                    error=str(exc),
                    detail="continuing; OrderManager polls REST for fills",
                )
                if private_ws is not None:
                    try:
                        private_ws.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    private_ws = None
        catchup = gap
        if not catchup:
            catchup = []
            for symbol in symbols:
                catchup.extend(
                    market.candles(symbol, config.trading.timeframe, limit=5, include_unclosed=False)
                )
        engine.ingest_history(catchup)
        if client is not None:
            apply_leverage(config, client, symbols, dry_run=engine.dry_run)
        deadline = None if seconds is None else time_fn() + seconds
        while True:
            if deadline is not None and time_fn() >= deadline:
                break
            if ws.trading_paused and str(getattr(ws.state, "value", ws.state)) == "failed":
                raise ConnectionLostError("public websocket failed; new entries paused")
            sleep_fn(0.2)
        snapshot = engine.snapshot(mark=engine.last_mark_candle())
        snapshot["disclaimer"] = TESTNET_DISCLAIMER
        return snapshot
    finally:
        if private_ws is not None:
            private_ws.stop()
        ws.stop()
        engine.set_paused(True, reason="live session stopped")
