from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from trading_bot.config.models import AppConfig
from trading_bot.core.exceptions import ConnectionLostError, GeoRestrictedError, TradingBotError
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.database.database import Database
from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.exchange.instruments import Instrument
from trading_bot.exchange.websocket import BybitWebSocket, StreamKind, kline_topic
from trading_bot.market.candles import Candle, to_bybit_interval
from trading_bot.market.market_data import MarketDataService
from trading_bot.monitoring.logger import get_logger
from trading_bot.paper.engine import PAPER_DISCLAIMER, PaperEngine
from trading_bot.risk.risk_manager import RiskManager
from trading_bot.strategy.base import Strategy

logger = get_logger("trading_bot.paper.runner")


def build_paper_engine(
    config: AppConfig,
    *,
    strategy: Strategy,
    risk: RiskManager,
    instruments: dict[str, Instrument],
    database: Database,
    kill_switch: KillSwitch,
    session_id: str,
) -> PaperEngine:
    return PaperEngine(
        config,
        strategy,
        risk,
        instruments,
        database=database,
        kill_switch=kill_switch,
        session_id=session_id,
    )


def split_history_and_gap(candles: list[Candle], last_ms: dict[str, int]) -> tuple[list[Candle], list[Candle]]:
    history: list[Candle] = []
    gap: list[Candle] = []
    for candle in candles:
        if not candle.confirmed:
            continue
        cursor = last_ms.get(candle.symbol, 0)
        if cursor == 0 or candle.start_ms <= cursor:
            history.append(candle)
        else:
            gap.append(candle)
    return history, gap


def run_replay(engine: PaperEngine, candles: list[Candle]) -> dict[str, Any]:
    engine.set_paused(False, reason="replay")
    engine.replay(candles)
    mark = candles[-1] if candles else None
    snapshot = engine.snapshot(mark=mark)
    snapshot["disclaimer"] = PAPER_DISCLAIMER
    return snapshot


def run_live(
    config: AppConfig,
    engine: PaperEngine,
    *,
    market: MarketDataService,
    symbols: list[str],
    seconds: float | None,
    warmup: int,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.time,
    websocket_factory: Callable[..., BybitWebSocket] | None = None,
) -> dict[str, Any]:
    """Public kline stream + REST warmup. Never sends orders."""
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
            logger.warning("paper_warmup_failed", symbol=symbol, error=str(exc))
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
    try:
        ws.wait_connected(timeout=20)
        engine.set_paused(False, reason="websocket connected")
        if gap:
            engine.ingest_confirmed(gap)
        else:
            # Re-fetch so bars that closed between warmup and the socket are not skipped.
            catchup: list[Candle] = []
            for symbol in symbols:
                catchup.extend(
                    market.candles(symbol, config.trading.timeframe, limit=5, include_unclosed=False)
                )
            engine.ingest_confirmed(catchup)
        deadline = None if seconds is None else time_fn() + seconds
        while True:
            if deadline is not None and time_fn() >= deadline:
                break
            if ws.trading_paused and str(getattr(ws.state, "value", ws.state)) == "failed":
                raise ConnectionLostError("paper websocket failed; trading paused")
            sleep_fn(0.2)
        return engine.snapshot(mark=engine.last_mark_candle())
    finally:
        ws.stop()
        engine.set_paused(True, reason="paper session stopped")
