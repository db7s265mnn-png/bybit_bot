from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.config.models import AppConfig, KillSwitchPolicy
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import ConnectionState, Side, SignalType
from trading_bot.database.database import Database
from trading_bot.exchange.instruments import parse_instrument
from trading_bot.execution.simulated import SimulatedBroker
from trading_bot.market.candles import Candle, parse_ws_kline_message
from trading_bot.paper.engine import PaperEngine
from trading_bot.paper.runner import run_live, split_history_and_gap
from trading_bot.risk.risk_manager import RiskManager
from trading_bot.strategy.base import Signal, Strategy, hold_signal
from tests.helpers import make_candles


class OneShotLong(Strategy):
    name = "oneshot"

    def required_history(self) -> int:
        return 1

    def generate_signal(self, history: Sequence[Candle]) -> Signal:
        last = history[-1]
        if len(history) != 1:
            return hold_signal(last.symbol, last.start_time)
        return Signal(
            type=SignalType.LONG,
            symbol=last.symbol,
            timestamp=last.start_time,
            confidence=Decimal("1"),
            stop_loss=Decimal("1"),
            take_profit=Decimal("100000"),
        )


def _instrument():
    return parse_instrument(
        "linear",
        {
            "symbol": "BTCUSDT",
            "status": "Trading",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {
                "qtyStep": "0.001",
                "minOrderQty": "0.001",
                "maxOrderQty": "10000",
                "maxMktOrderQty": "10000",
                "minNotionalValue": "5",
            },
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "100", "leverageStep": "0.01"},
        },
    )


def _engine(app_config: AppConfig, tmp_path, strategy=None, kill=None, session_id: str = "paper-test"):
    app_config.execution.slippage = Decimal("0")
    app_config.execution.spread = Decimal("0")
    app_config.fees.taker = Decimal("0")
    app_config.fees.maker = Decimal("0")
    app_config.fees.assumed_funding_rate = Decimal("0")
    app_config.risk.max_position_size = Decimal("1")
    db = Database(f"sqlite:///{tmp_path / 'paper.db'}")
    kill = kill or KillSwitch()
    engine = PaperEngine(
        app_config,
        strategy or OneShotLong(),
        RiskManager(app_config, kill),
        _instrument(),
        database=db,
        kill_switch=kill,
        session_id=session_id,
    )
    return engine, db, kill


def _pair() -> tuple[Candle, Candle]:
    c0 = Candle(
        symbol="BTCUSDT",
        interval="15",
        start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("90"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        confirmed=True,
    )
    c1 = Candle(
        symbol="BTCUSDT",
        interval="15",
        start_time=datetime(2024, 1, 1, 0, 15, tzinfo=timezone.utc),
        open=Decimal("110"),
        high=Decimal("111"),
        low=Decimal("109"),
        close=Decimal("110.5"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        confirmed=True,
    )
    return c0, c1


def test_paper_fill_uses_next_open_not_signal_close(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill = _engine(app_config, tmp_path)
    c0, c1 = _pair()
    engine.replay([c0, c1])
    pos = engine.broker.positions["BTCUSDT"]
    assert pos.entry_fill == Decimal("110.0") or pos.entry_fill == Decimal("110")
    assert pos.entry_fill != c0.close
    snap = engine.snapshot()
    assert snap["real_orders"] is False
    assert "does not guarantee" in snap["disclaimer"].lower()
    db.close()


def test_paper_pause_blocks_new_entries(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill = _engine(app_config, tmp_path)
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.set_paused(True, reason="unit-test")
    engine.ingest_confirmed([c1])
    assert engine.broker.positions == {}
    db.close()


def test_paper_kill_switch_blocks_entries(app_config: AppConfig, tmp_path) -> None:
    kill = KillSwitch()
    engine, db, kill = _engine(app_config, tmp_path, kill=kill)
    kill.activate("unit-test")
    c0, c1 = _pair()
    engine.replay([c0, c1])
    assert engine.broker.positions == {}
    db.close()


def test_paper_flatten_closes_virtual_position(app_config: AppConfig, tmp_path) -> None:
    kill = KillSwitch(KillSwitchPolicy.FLATTEN)
    engine, db, kill = _engine(app_config, tmp_path, kill=kill)
    c0, c1 = _pair()
    engine.replay([c0, c1])
    assert "BTCUSDT" in engine.broker.positions
    c2 = Candle(
        symbol="BTCUSDT",
        interval="15",
        start_time=datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc),
        open=Decimal("108"),
        high=Decimal("109"),
        low=Decimal("107"),
        close=Decimal("108.5"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        confirmed=True,
    )
    kill.activate("flatten-test")
    engine.ingest_confirmed([c2])
    assert engine.broker.positions == {}
    assert engine.broker.trades
    db.close()


def test_paper_restores_open_position(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill = _engine(app_config, tmp_path, session_id="restore-me")
    c0, c1 = _pair()
    engine.replay([c0, c1])
    assert "BTCUSDT" in engine.broker.positions
    engine2 = PaperEngine(
        app_config,
        OneShotLong(),
        RiskManager(app_config, KillSwitch()),
        _instrument(),
        database=db,
        kill_switch=KillSwitch(),
        session_id="restore-me",
    )
    assert engine2.restore() is True
    assert "BTCUSDT" in engine2.broker.positions
    assert engine2.broker.positions["BTCUSDT"].quantity == engine.broker.positions["BTCUSDT"].quantity
    db.close()


def test_limit_order_fills_when_traded_through(app_config: AppConfig, tmp_path) -> None:
    app_config.execution.slippage = Decimal("0")
    app_config.fees.taker = Decimal("0")
    app_config.fees.maker = Decimal("0")
    app_config.risk.max_position_size = Decimal("1")
    db = Database(f"sqlite:///{tmp_path / 'lim.db'}")
    risk = RiskManager(app_config, KillSwitch())
    broker = SimulatedBroker(app_config, _instrument(), strategy_name="hold", database=db, session_id="lim")
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    order = broker.place_limit(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.1"),
        price=Decimal("100"),
        stop_loss=Decimal("90"),
        take_profit=Decimal("130"),
        now=now,
        risk=risk,
        allow=True,
    )
    assert order.status == "New"
    candle = Candle(
        symbol="BTCUSDT",
        interval="15",
        start_time=now,
        open=Decimal("105"),
        high=Decimal("106"),
        low=Decimal("99"),
        close=Decimal("104"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        confirmed=True,
    )
    broker.process_market_candle(candle, risk, allow_entries=True)
    assert "BTCUSDT" in broker.positions
    assert broker.positions["BTCUSDT"].entry_ref == Decimal("100")
    db.close()


def test_ws_kline_confirm_false_is_ignored_by_parser_contract() -> None:
    payload = {
        "topic": "kline.15.BTCUSDT",
        "data": [
            {
                "start": 1704067200000,
                "interval": "15",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100.5",
                "volume": "1",
                "turnover": "1",
                "confirm": False,
            }
        ],
    }
    candles = parse_ws_kline_message(payload)
    assert len(candles) == 1
    assert candles[0].confirmed is False
    payload["data"][0]["confirm"] = True
    assert parse_ws_kline_message(payload)[0].confirmed is True


def test_split_history_and_gap_keeps_cursor() -> None:
    candles = make_candles([100, 101, 102])
    history, gap = split_history_and_gap(candles, {candles[0].symbol: candles[1].start_ms})
    assert [c.start_ms for c in history] == [candles[0].start_ms, candles[1].start_ms]
    assert [c.start_ms for c in gap] == [candles[2].start_ms]


class _FakeMarket:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles_data = candles
        self.calls = 0

    def candles(self, symbol: str, timeframe: str, *, limit: int = 200, include_unclosed: bool = False):
        self.calls += 1
        return list(self.candles_data[-limit:])


class _FakeWS:
    def __init__(self, config, kind=None, on_message=None, on_state=None):
        self.on_message = on_message
        self.on_state = on_state
        self.trading_paused = False
        self.state = ConnectionState.CONNECTED
        self.started = False
        self.stopped = False
        self.topics: list[str] = []

    def subscribe(self, topics: list[str]) -> None:
        self.topics = list(topics)

    def start(self) -> None:
        self.started = True
        if self.on_state:
            self.on_state(ConnectionState.CONNECTED, "connected")

    def wait_connected(self, timeout: float = 15.0) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True


def test_live_runner_never_needs_order_api(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill = _engine(app_config, tmp_path)
    c0, c1 = _pair()
    market = _FakeMarket([c0])
    ticks = {"t": 0.0}

    def time_fn() -> float:
        return ticks["t"]

    def sleep_fn(_seconds: float) -> None:
        ticks["t"] += 1.0
        if engine.paused is False and engine.broker.pending:
            engine.handle_ws_message(
                {
                    "topic": "kline.15.BTCUSDT",
                    "data": [
                        {
                            "start": c1.start_ms,
                            "interval": "15",
                            "open": str(c1.open),
                            "high": str(c1.high),
                            "low": str(c1.low),
                            "close": str(c1.close),
                            "volume": "1",
                            "turnover": "1",
                            "confirm": True,
                        }
                    ],
                }
            )

    snapshot = run_live(
        app_config,
        engine,
        market=market,
        symbols=["BTCUSDT"],
        seconds=1.0,
        warmup=10,
        sleep_fn=sleep_fn,
        time_fn=time_fn,
        websocket_factory=_FakeWS,
    )
    assert snapshot["real_orders"] is False
    assert "BTCUSDT" in engine.broker.positions
    db.close()
