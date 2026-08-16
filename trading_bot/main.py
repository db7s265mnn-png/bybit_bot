from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from trading_bot.account.service import AccountService
from trading_bot.backtest.engine import DISCLAIMER, resolve_instrument
from trading_bot.backtest.runner import run_backtest_suite
from trading_bot.config.loader import load_config
from trading_bot.config.models import AppConfig, TradingMode
from trading_bot.core.exceptions import GeoRestrictedError, TradingBotError
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.redaction import redact_obj
from trading_bot.database.database import Database
from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.exchange.websocket import (
    BybitWebSocket,
    StreamKind,
    kline_topic,
    orderbook_topic,
    ticker_topic,
)
from trading_bot.market.candles import to_bybit_interval
from trading_bot.market.csv_io import candles_from_csv
from trading_bot.market.market_data import MarketDataService
from trading_bot.market.store import CandleStore
from trading_bot.monitoring.logger import configure_logging, get_logger
from trading_bot.paper.engine import PAPER_DISCLAIMER, PaperEngine
from trading_bot.paper.runner import run_live, run_replay
from trading_bot.risk.risk_manager import RiskManager
from trading_bot.strategy import create_strategy

MAINNET_BANNER = """
WARNING:
REAL MONEY TRADING ENABLED
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _print(data: Any) -> None:
    print(json.dumps(redact_obj(data), indent=2, default=_json_default, ensure_ascii=False))


def _warn_mainnet(config: AppConfig) -> None:
    if config.system.mode is TradingMode.MAINNET:
        print(MAINNET_BANNER.strip(), file=sys.stderr)
        if not config.system.live_trading_confirm:
            print(
                "LIVE_TRADING_CONFIRM is not true — real orders are blocked.",
                file=sys.stderr,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Modular Bybit V5 trading bot",
    )
    parser.add_argument("--config", help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Check REST connectivity and server time")
    sub.add_parser("instruments", help="Fetch tick/lot/min qty from the API")
    kline = sub.add_parser("klines", help="Fetch recent candles")
    kline.add_argument("--symbol", default=None)
    kline.add_argument("--limit", type=int, default=5)
    kline.add_argument("--include-unclosed", action="store_true")
    tick = sub.add_parser("ticker", help="Fetch ticker / spread")
    tick.add_argument("--symbol", default=None)
    book = sub.add_parser("orderbook", help="Fetch order book")
    book.add_argument("--symbol", default=None)
    book.add_argument("--limit", type=int, default=5)
    sub.add_parser("account", help="Fetch wallet, positions, orders, API key permissions")
    stream = sub.add_parser("stream", help="Subscribe to public WebSocket for a few seconds")
    stream.add_argument("--seconds", type=float, default=12.0)
    sync = sub.add_parser("sync-candles", help="Fetch candles from Bybit and store them in SQLite")
    sync.add_argument("--symbol", default=None)
    sync.add_argument("--limit", type=int, default=500)
    backtest = sub.add_parser("backtest", help="Run event-driven backtest (fees/slippage/funding included)")
    backtest.add_argument("--symbol", default=None)
    backtest.add_argument("--limit", type=int, default=500)
    backtest.add_argument("--csv", default=None, help="OHLCV CSV (start_ms,open,high,low,close,volume)")
    backtest.add_argument("--from-db", action="store_true")
    backtest.add_argument("--persist", action="store_true", help="Write full-run trades into SQLite")
    signal = sub.add_parser("signal", help="Compute the latest strategy signal from stored/fetched candles")
    signal.add_argument("--symbol", default=None)
    signal.add_argument("--limit", type=int, default=200)
    signal.add_argument("--csv", default=None)
    signal.add_argument("--from-db", action="store_true")
    paper = sub.add_parser("paper", help="Paper trade: live klines or CSV replay, never sends orders")
    paper.add_argument("--symbol", default=None)
    paper.add_argument("--seconds", type=float, default=None, help="Stop live paper after N seconds (default: until interrupt)")
    paper.add_argument("--csv", default=None, help="Replay OHLCV through the paper engine (no Bybit, no real orders)")
    paper.add_argument("--from-db", action="store_true", help="Replay stored SQLite candles through the paper engine")
    paper.add_argument("--session", default="paper", help="Paper session id for SQLite restore")
    paper.add_argument("--warmup", type=int, default=200, help="Confirmed candles to seed EMA before live paper")
    return parser


def cmd_ping(client: BybitRESTClient) -> int:
    sync = client.get_server_time()
    _print(
        {
            "host": client.host_label(),
            "server_time_ms": sync.server_time_ms,
            "offset_ms": sync.offset_ms,
            "clock_skew_warning": sync.skew_too_large,
        }
    )
    return 0


def cmd_instruments(client: BybitRESTClient) -> int:
    rows = []
    for instrument in client.get_instruments():
        rows.append(
            {
                "symbol": instrument.symbol,
                "status": instrument.status,
                "tick_size": instrument.tick_size,
                "qty_step": instrument.qty_step,
                "min_order_qty": instrument.min_order_qty,
                "max_order_qty": instrument.max_order_qty,
                "max_mkt_order_qty": instrument.max_mkt_order_qty,
                "min_notional": instrument.min_notional,
                "min_leverage": instrument.min_leverage,
                "max_leverage": instrument.max_leverage,
            }
        )
    _print(rows)
    return 0


def cmd_klines(config: AppConfig, market: MarketDataService, symbol: str, limit: int, include_unclosed: bool) -> int:
    candles = market.candles(
        symbol,
        config.trading.timeframe,
        limit=limit,
        include_unclosed=include_unclosed,
    )
    _print(
        [
            {
                "start": candle.start_time.isoformat(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "confirmed": candle.confirmed,
            }
            for candle in candles
        ]
    )
    return 0


def cmd_ticker(market: MarketDataService, symbol: str) -> int:
    ticker = market.ticker(symbol)
    _print(
        {
            "symbol": ticker.symbol,
            "last": ticker.last_price,
            "mark": ticker.mark_price,
            "bid": ticker.bid1,
            "ask": ticker.ask1,
            "spread": ticker.spread,
            "funding_rate": ticker.funding_rate,
        }
    )
    return 0


def cmd_orderbook(market: MarketDataService, symbol: str, limit: int) -> int:
    book = market.orderbook(symbol, limit=limit)
    _print(
        {
            "symbol": book.symbol,
            "best_bid": None if not book.best_bid else [book.best_bid.price, book.best_bid.qty],
            "best_ask": None if not book.best_ask else [book.best_ask.price, book.best_ask.qty],
            "spread": book.spread,
            "spread_pct": book.spread_pct,
            "mid": book.mid,
        }
    )
    return 0


def cmd_account(client: BybitRESTClient) -> int:
    snapshot = AccountService(client).snapshot()
    _print(
        {
            "api_key": {
                "note": snapshot.api_key.note,
                "read_only": snapshot.api_key.read_only,
                "uta": snapshot.api_key.uta,
                "ip_whitelist": bool(snapshot.api_key.ips) and snapshot.api_key.ips != ("*",),
                "has_withdraw": snapshot.api_key.has_withdraw,
                "wallet_permissions": snapshot.api_key.wallet_permissions,
            },
            "wallet": {
                "account_type": snapshot.wallet.account_type,
                "total_equity": snapshot.wallet.total_equity,
                "total_available_balance": snapshot.wallet.total_available_balance,
                "total_perp_upl": snapshot.wallet.total_perp_upl,
                "coins": [
                    {
                        "coin": coin.coin,
                        "equity": coin.equity,
                        "wallet_balance": coin.wallet_balance,
                        "usd_value": coin.usd_value,
                    }
                    for coin in snapshot.wallet.coins
                ],
            },
            "positions": [
                {
                    "symbol": pos.symbol,
                    "side": pos.side,
                    "size": pos.size,
                    "avg_price": pos.avg_price,
                    "unrealised_pnl": pos.unrealised_pnl,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                }
                for pos in snapshot.positions
            ],
            "open_orders": [
                {
                    "order_id": order.order_id,
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "type": order.order_type,
                    "status": order.status,
                    "qty": order.qty,
                    "stop_order_type": order.stop_order_type,
                }
                for order in snapshot.open_orders
            ],
        }
    )
    return 0


def cmd_stream(config: AppConfig, seconds: float) -> int:
    log = get_logger("trading_bot.stream")
    received: list[str] = []

    def on_message(payload: dict[str, Any]) -> None:
        topic = str(payload.get("topic") or payload.get("op") or "unknown")
        received.append(topic)
        log.info("ws_message", topic=topic, type=payload.get("type"))

    def on_state(state: Any, reason: str) -> None:
        log.info("ws_state_callback", state=getattr(state, "value", state), reason=reason)

    interval = to_bybit_interval(config.trading.timeframe)
    topics = []
    for symbol in config.exchange.symbols:
        topics.extend(
            [
                ticker_topic(symbol),
                kline_topic(interval, symbol),
                orderbook_topic(symbol, 1),
            ]
        )
    ws = BybitWebSocket(config, kind=StreamKind.PUBLIC, on_message=on_message, on_state=on_state)
    ws.subscribe(topics)
    ws.start()
    try:
        ws.wait_connected(timeout=20)
        deadline = time.time() + seconds
        while time.time() < deadline:
            if ws.trading_paused and ws.state.value == "failed":
                return 1
            time.sleep(0.2)
        _print(
            {
                "state": ws.state.value,
                "trading_paused": ws.trading_paused,
                "messages": len(received),
                "topics_seen": sorted(set(received)),
                "last_message_at": None if ws.last_message_at is None else ws.last_message_at.isoformat(),
            }
        )
        return 0 if received else 1
    finally:
        ws.stop()


def _open_db(config: AppConfig) -> Database:
    return Database(config.database.url)


def _load_candles(
    config: AppConfig,
    symbol: str,
    *,
    limit: int,
    csv_path: str | None,
    from_db: bool,
    client: BybitRESTClient | None,
) -> list:
    if csv_path:
        return candles_from_csv(csv_path, symbol, config.trading.timeframe)
    db = _open_db(config)
    try:
        if from_db:
            stored = CandleStore(db).load(symbol, config.trading.timeframe)
            if not stored:
                raise TradingBotError("SQLite has no candles; run sync-candles or pass --csv")
            return stored[-limit:] if limit else stored
        if client is None:
            raise TradingBotError("Bybit client is required unless --csv or --from-db is set")
        market = MarketDataService(client)
        store = CandleStore(db, market)
        try:
            instrument = client.get_instrument(symbol)
        except TradingBotError:
            instrument = None
        return store.sync(symbol, config.trading.timeframe, limit=limit, instrument=instrument)
    finally:
        db.close()


def cmd_sync_candles(config: AppConfig, client: BybitRESTClient, symbol: str, limit: int) -> int:
    db = _open_db(config)
    try:
        store = CandleStore(db, MarketDataService(client))
        instrument = client.get_instrument(symbol)
        candles = store.sync(symbol, config.trading.timeframe, limit=limit, instrument=instrument)
        _print(
            {
                "symbol": symbol,
                "stored": len(candles),
                "first": None if not candles else candles[0].start_time.isoformat(),
                "last": None if not candles else candles[-1].start_time.isoformat(),
            }
        )
        return 0
    finally:
        db.close()


def cmd_backtest(
    config: AppConfig,
    symbol: str,
    *,
    limit: int,
    csv_path: str | None,
    from_db: bool,
    persist: bool,
    client: BybitRESTClient | None,
) -> int:
    print(DISCLAIMER, file=sys.stderr)
    candles = _load_candles(config, symbol, limit=limit, csv_path=csv_path, from_db=from_db, client=client)
    db = _open_db(config) if persist or from_db else None
    try:
        instrument, source = resolve_instrument(config, symbol, db)
        if client is not None:
            try:
                instrument = client.get_instrument(symbol)
                source = "api"
                if db is not None:
                    db.save_instrument(instrument)
            except (TradingBotError, GeoRestrictedError):
                pass
        report = run_backtest_suite(
            config,
            candles,
            instrument=instrument,
            database=db,
            persist_full=persist,
        )
        report["instrument_source"] = source
        _print(report)
        return 0
    finally:
        if db is not None:
            db.close()


def cmd_signal(
    config: AppConfig,
    symbol: str,
    *,
    limit: int,
    csv_path: str | None,
    client: BybitRESTClient | None,
    from_db: bool = False,
) -> int:
    candles = _load_candles(
        config,
        symbol,
        limit=limit,
        csv_path=csv_path,
        from_db=from_db or (csv_path is None and client is None),
        client=client,
    )
    strategy = create_strategy(config.strategy.name, params=config.strategy.params)
    if len(candles) < strategy.required_history():
        raise TradingBotError(f"need at least {strategy.required_history()} candles, got {len(candles)}")
    signal = strategy.generate_signal(candles)
    _print(
        {
            "type": signal.type.value,
            "symbol": signal.symbol,
            "timestamp": signal.timestamp.isoformat(),
            "confidence": signal.confidence,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "recommended_size": signal.recommended_size,
            "extra": signal.extra,
        }
    )
    return 0


def cmd_paper(
    config: AppConfig,
    *,
    symbols: list[str],
    seconds: float | None,
    csv_path: str | None,
    from_db: bool,
    session_id: str,
    warmup: int,
    client: BybitRESTClient | None,
    kill_switch: KillSwitch,
) -> int:
    print(PAPER_DISCLAIMER, file=sys.stderr)
    db = _open_db(config)
    engine: PaperEngine | None = None
    try:
        instruments = {}
        for sym in symbols:
            instrument, _source = resolve_instrument(config, sym, db)
            if client is not None:
                try:
                    instrument = client.get_instrument(sym)
                    db.save_instrument(instrument)
                except (TradingBotError, GeoRestrictedError):
                    pass
            instruments[sym] = instrument
        strategy = create_strategy(config.strategy.name, params=config.strategy.params)
        risk = RiskManager(config, kill_switch)
        engine = PaperEngine(
            config,
            strategy,
            risk,
            instruments,
            database=db,
            kill_switch=kill_switch,
            session_id=session_id,
        )
        engine.restore()
        if csv_path:
            candles = candles_from_csv(csv_path, symbols[0], config.trading.timeframe)
            snapshot = run_replay(engine, candles)
        elif from_db:
            store = CandleStore(db)
            candles = []
            for sym in symbols:
                candles.extend(store.load(sym, config.trading.timeframe))
            if not candles:
                raise TradingBotError("SQLite has no candles; run sync-candles or pass --csv")
            candles.sort(key=lambda item: (item.start_ms, item.symbol))
            snapshot = run_replay(engine, candles)
        else:
            if client is None:
                raise TradingBotError("live paper needs Bybit connectivity, or pass --csv / --from-db")
            snapshot = run_live(
                config,
                engine,
                market=MarketDataService(client),
                symbols=symbols,
                seconds=seconds,
                warmup=warmup,
            )
        snapshot["real_orders"] = False
        _print(snapshot)
        return 0
    except KeyboardInterrupt:
        if engine is not None:
            _print(engine.snapshot(mark=engine.last_mark_candle()))
        return 0
    finally:
        db.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    configure_logging(config.system.log_level)
    log = get_logger("trading_bot")
    log.info("startup", **config.public_summary())
    _warn_mainnet(config)
    kill_switch = KillSwitch(config.kill_switch.position_policy)
    log.debug("kill_switch_ready", active=kill_switch.is_active())

    symbol = getattr(args, "symbol", None) or config.exchange.symbols[0]
    needs_client = args.command in {
        "ping",
        "instruments",
        "klines",
        "ticker",
        "orderbook",
        "account",
        "stream",
        "sync-candles",
    }
    if args.command in {"backtest", "signal", "paper"}:
        needs_client = not getattr(args, "csv", None) and not getattr(args, "from_db", False)
    client = BybitRESTClient(config) if needs_client else None
    market = MarketDataService(client) if client is not None else None

    commands = {
        "ping": lambda: cmd_ping(client),
        "instruments": lambda: cmd_instruments(client),
        "klines": lambda: cmd_klines(
            config, market, symbol, args.limit, args.include_unclosed
        ),
        "ticker": lambda: cmd_ticker(market, symbol),
        "orderbook": lambda: cmd_orderbook(market, symbol, args.limit),
        "account": lambda: cmd_account(client),
        "stream": lambda: cmd_stream(config, args.seconds),
        "sync-candles": lambda: cmd_sync_candles(config, client, symbol, args.limit),
        "backtest": lambda: cmd_backtest(
            config,
            symbol,
            limit=args.limit,
            csv_path=args.csv,
            from_db=args.from_db,
            persist=args.persist,
            client=client,
        ),
        "signal": lambda: cmd_signal(
            config,
            symbol,
            limit=args.limit,
            csv_path=args.csv,
            client=client,
            from_db=getattr(args, "from_db", False),
        ),
        "paper": lambda: cmd_paper(
            config,
            symbols=[symbol] if getattr(args, "symbol", None) else list(config.exchange.symbols),
            seconds=args.seconds,
            csv_path=args.csv,
            from_db=args.from_db,
            session_id=args.session,
            warmup=args.warmup,
            client=client,
            kill_switch=kill_switch,
        ),
    }
    try:
        return commands[args.command]()
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 0
    except GeoRestrictedError as exc:
        log.error(
            "command_failed",
            command=args.command,
            error=str(exc),
            error_type=type(exc).__name__,
            hint="Bybit blocked this IP. Replay with --csv/--from-db or run from an allowed region.",
        )
        return 1
    except TradingBotError as exc:
        log.error(
            "command_failed",
            command=args.command,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
