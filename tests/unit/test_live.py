from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_bot.account.service import AccountService, has_protective_sl
from trading_bot.config.models import AppConfig, KillSwitchPolicy, TradingMode
from trading_bot.core.exceptions import TradingBotError
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import ConnectionState, SignalType
from trading_bot.database.database import Database, TradeRecord
from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.exchange.instruments import parse_instrument
from trading_bot.execution.order_manager import OrderManager
from trading_bot.live.engine import TESTNET_DISCLAIMER, LiveEngine, require_testnet_live
from trading_bot.live.runner import apply_leverage, run_testnet_loop
from trading_bot.market.candles import Candle
from trading_bot.risk.risk_manager import RiskManager
from trading_bot.strategy.base import Signal, Strategy
from tests.helpers import make_candles


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class AlwaysLong(Strategy):
    name = "always-long"

    def required_history(self) -> int:
        return 1

    def generate_signal(self, history: Sequence[Candle]) -> Signal:
        last = history[-1]
        return Signal(
            type=SignalType.LONG,
            symbol=last.symbol,
            timestamp=last.start_time,
            confidence=Decimal("1"),
            stop_loss=last.close * Decimal("0.9"),
            take_profit=last.close * Decimal("1.2"),
        )


class AlwaysShort(Strategy):
    name = "always-short"

    def required_history(self) -> int:
        return 1

    def generate_signal(self, history: Sequence[Candle]) -> Signal:
        last = history[-1]
        return Signal(
            type=SignalType.SHORT,
            symbol=last.symbol,
            timestamp=last.start_time,
            confidence=Decimal("1"),
            stop_loss=last.close * Decimal("2"),
            take_profit=last.close * Decimal("0.5"),
        )


class FakeLiveHTTP:
    def __init__(self) -> None:
        self.place_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.stop_calls: list[dict] = []
        self.leverage_calls: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.status_queue: dict[str, list[str]] = {}
        self.positions: dict[str, dict] = {}
        self.wallet_equity = "10000"
        self.seq = 0
        self.api_key_calls = 0
        self.leverage_unchanged = False
        self.apply_sl_on_stop = True

    def set_position(
        self,
        symbol: str = "BTCUSDT",
        *,
        side: str = "Buy",
        size: str = "0.01",
        sl: str = "90000",
        avg: str = "100000",
        tp: str = "",
    ) -> None:
        self.positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "size": size,
            "avgPrice": avg,
            "stopLoss": sl,
            "takeProfit": tp,
            "positionIdx": 0,
            "unrealisedPnl": "0",
        }

    def get_wallet_balance(self, **_kwargs) -> dict:
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "totalEquity": self.wallet_equity,
                        "totalWalletBalance": self.wallet_equity,
                        "totalAvailableBalance": self.wallet_equity,
                        "totalPerpUPL": "0",
                        "coin": [
                            {
                                "coin": "USDT",
                                "equity": self.wallet_equity,
                                "walletBalance": self.wallet_equity,
                                "usdValue": self.wallet_equity,
                                "unrealisedPnl": "0",
                                "locked": "0",
                            }
                        ],
                    }
                ]
            },
        }

    def get_api_key_information(self, **_kwargs) -> dict:
        self.api_key_calls += 1
        return {
            "retCode": 0,
            "result": {
                "note": "bot",
                "readOnly": 0,
                "uta": 1,
                "ips": ["127.0.0.1"],
                "permissions": {"Wallet": ["AccountTransfer"], "ContractTrade": ["Order"]},
            },
        }

    def get_fee_rates(self, **_kwargs) -> dict:
        return {"retCode": 0, "result": {"list": []}}

    def get_positions(self, **kwargs) -> dict:
        symbol = kwargs.get("symbol")
        rows = []
        for pos in self.positions.values():
            if Decimal(str(pos.get("size") or "0")) <= 0:
                continue
            if symbol and pos["symbol"] != symbol:
                continue
            rows.append(pos)
        return {"retCode": 0, "result": {"list": rows}}

    def place_order(self, **kwargs) -> dict:
        self.place_calls.append(kwargs)
        self.seq += 1
        link = kwargs["orderLinkId"]
        oid = f"ex-{self.seq}"
        self.orders[link] = {
            "orderId": oid,
            "orderLinkId": link,
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "orderType": kwargs["orderType"],
            "qty": kwargs["qty"],
            "orderStatus": "Created",
            "cumExecQty": "0",
            "avgPrice": "",
            "reduceOnly": kwargs.get("reduceOnly", False),
            "stopLoss": kwargs.get("stopLoss") or "",
            "takeProfit": kwargs.get("takeProfit") or "",
            "rejectReason": "",
        }
        self.status_queue.setdefault(link, ["New", "Filled"])
        return {"retCode": 0, "result": {"orderId": oid, "orderLinkId": link, "orderStatus": "Created"}}

    def _apply_fill(self, row: dict) -> None:
        symbol = row["symbol"]
        if row.get("reduceOnly"):
            self.positions.pop(symbol, None)
            return
        sl = row.get("stopLoss") or ""
        existing = self.positions.get(symbol) or {}
        self.positions[symbol] = {
            "symbol": symbol,
            "side": row["side"],
            "size": row["qty"],
            "avgPrice": row.get("avgPrice") or "100000",
            "stopLoss": sl or existing.get("stopLoss") or "",
            "takeProfit": row.get("takeProfit") or "",
            "positionIdx": 0,
            "unrealisedPnl": "0",
        }

    def _advance(self, link: str) -> dict | None:
        row = self.orders.get(link)
        if row is None:
            return None
        queue = self.status_queue.get(link) or []
        if queue:
            status = queue.pop(0)
            row["orderStatus"] = status
            if status == "Filled":
                row["cumExecQty"] = row["qty"]
                row["avgPrice"] = row.get("avgPrice") or "100000"
                self._apply_fill(row)
            elif status == "PartiallyFilled":
                row["cumExecQty"] = "0.005"
                row["avgPrice"] = "100000"
        return row

    def get_open_orders(self, **kwargs) -> dict:
        link = kwargs.get("orderLinkId")
        if link:
            row = self._advance(link)
            if row and row["orderStatus"] in {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled"}:
                return {"retCode": 0, "result": {"list": []}}
            return {"retCode": 0, "result": {"list": [row] if row else []}}
        working = [
            row
            for row in self.orders.values()
            if row["orderStatus"] in {"Created", "New", "PartiallyFilled"}
        ]
        return {"retCode": 0, "result": {"list": working}}

    def get_order_history(self, **kwargs) -> dict:
        link = kwargs.get("orderLinkId")
        row = self.orders.get(link)
        if row and row["orderStatus"] in {
            "Filled",
            "Cancelled",
            "Rejected",
            "PartiallyFilledCanceled",
            "PartiallyFilled",
        }:
            return {"retCode": 0, "result": {"list": [row]}}
        return {"retCode": 0, "result": {"list": []}}

    def cancel_order(self, **kwargs) -> dict:
        self.cancel_calls.append(kwargs)
        link = kwargs.get("orderLinkId")
        if link and link in self.orders:
            self.orders[link]["orderStatus"] = "Cancelled"
        return {"retCode": 0, "result": {"orderLinkId": link}}

    def set_trading_stop(self, **kwargs) -> dict:
        self.stop_calls.append(kwargs)
        symbol = kwargs.get("symbol")
        if self.apply_sl_on_stop and symbol in self.positions and kwargs.get("stopLoss"):
            self.positions[symbol]["stopLoss"] = str(kwargs["stopLoss"])
        return {"retCode": 0, "result": {}}

    def set_leverage(self, **kwargs) -> dict:
        self.leverage_calls.append(kwargs)
        if self.leverage_unchanged:
            return {"retCode": 110043, "retMsg": "leverage not modified"}
        return {"retCode": 0, "result": {}}


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


def _testnet(app_config: AppConfig) -> AppConfig:
    app_config.system.mode = TradingMode.TESTNET
    app_config.exchange.testnet = True
    app_config.secrets.bybit_api_key = "test-key"
    app_config.secrets.bybit_api_secret = "test-secret"
    app_config.orders.fill_timeout_sec = 5.0
    app_config.orders.poll_interval_sec = 0.5
    app_config.orders.sl_confirm_attempts = 3
    app_config.orders.sl_confirm_delay_sec = 0.5
    app_config.execution.slippage = Decimal("0")
    app_config.execution.spread = Decimal("0")
    app_config.fees.taker = Decimal("0")
    app_config.fees.maker = Decimal("0")
    app_config.risk.max_position_size = Decimal("1")
    return app_config


def _engine(
    app_config: AppConfig,
    tmp_path,
    http: FakeLiveHTTP | None = None,
    *,
    strategy=None,
    kill=None,
    session_id: str = "live-test",
    dry_run: bool = False,
):
    cfg = _testnet(app_config)
    http = http or FakeLiveHTTP()
    clock = Clock()
    db = Database(f"sqlite:///{tmp_path / 'live.db'}")
    client = BybitRESTClient(cfg, http=http)
    kill = kill or KillSwitch()
    orders = OrderManager(
        cfg,
        client,
        database=db,
        kill_switch=kill,
        session_id=session_id,
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    engine = LiveEngine(
        cfg,
        strategy or AlwaysLong(),
        RiskManager(cfg, kill),
        _instrument(),
        account=AccountService(client),
        orders=orders,
        database=db,
        kill_switch=kill,
        session_id=session_id,
        dry_run=dry_run,
    )
    return engine, db, kill, http, client


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


def test_live_cli_requires_testnet(app_config: AppConfig) -> None:
    with pytest.raises(TradingBotError, match="testnet"):
        require_testnet_live(app_config)


def test_live_cli_refuses_mainnet(app_config: AppConfig) -> None:
    app_config.system.mode = TradingMode.MAINNET
    app_config.exchange.testnet = False
    with pytest.raises(TradingBotError, match="Phase 10"):
        require_testnet_live(app_config)


def test_restore_uses_exchange_not_sqlite(app_config: AppConfig, tmp_path) -> None:
    http = FakeLiveHTTP()
    http.set_position("ETHUSDT", side="Buy", size="0.2", sl="1800", avg="2000")
    engine, db, _kill, http, _client = _engine(app_config, tmp_path, http)
    db.upsert_trade(
        TradeRecord(
            trade_id="sqlite-ghost",
            symbol="BTCUSDT",
            side="Buy",
            entry_price=Decimal("100"),
            exit_price=None,
            quantity=Decimal("9"),
            entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            exit_time=None,
            gross_pnl=None,
            fees=None,
            funding=None,
            slippage=None,
            net_pnl=None,
            strategy="ghost",
            session_id="live-test",
        )
    )
    engine.restore()
    assert "ETHUSDT" in engine.positions
    assert "BTCUSDT" not in engine.positions
    assert engine.positions["ETHUSDT"].size == Decimal("0.2")
    assert http.api_key_calls == 1
    db.close()


def test_restore_missing_sl_flattens(app_config: AppConfig, tmp_path) -> None:
    http = FakeLiveHTTP()
    http.set_position("BTCUSDT", sl="")
    engine, db, _kill, http, _client = _engine(app_config, tmp_path, http)
    engine.restore()
    assert "BTCUSDT" not in engine.positions
    assert any(call.get("reduceOnly") is True for call in http.place_calls)
    db.close()


def test_pause_blocks_new_entries(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, _client = _engine(app_config, tmp_path)
    engine.restore()
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.set_paused(True, reason="unit-test")
    engine.ingest_confirmed([c1])
    assert http.place_calls == []
    assert engine.positions == {}
    db.close()


def test_signal_on_n_order_on_n_plus_1(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, _client = _engine(app_config, tmp_path)
    engine.restore()
    engine.set_paused(False, reason="unit-test")
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    assert http.place_calls == []
    assert "BTCUSDT" in engine.pending
    engine.ingest_confirmed([c1])
    assert http.place_calls
    assert http.place_calls[0].get("reduceOnly") is not True
    assert "BTCUSDT" in engine.positions
    assert has_protective_sl(engine.positions["BTCUSDT"])
    db.close()


def test_warmup_and_catchup_do_not_place(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, _client = _engine(app_config, tmp_path)
    engine.restore()
    engine.set_paused(False, reason="unit-test")
    bars = make_candles([100, 101, 102, 103, 104])
    engine.load_history(bars)
    engine.seed_if_fresh()
    engine.ingest_history(bars)
    assert http.place_calls == []
    live = make_candles([105], start=datetime(2024, 1, 1, 1, 15, tzinfo=timezone.utc))
    engine.ingest_confirmed(live)
    assert len(http.place_calls) == 1
    db.close()


def test_kill_switch_blocks_entry(app_config: AppConfig, tmp_path) -> None:
    kill = KillSwitch()
    engine, db, kill, http, _client = _engine(app_config, tmp_path, kill=kill)
    kill.activate("unit-test")
    engine.restore()
    engine.set_paused(False, reason="unit-test")
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.ingest_confirmed([c1])
    assert http.place_calls == []
    db.close()


def test_kill_switch_flatten_closes_exchange_position(app_config: AppConfig, tmp_path) -> None:
    kill = KillSwitch(KillSwitchPolicy.FLATTEN)
    engine, db, kill, http, _client = _engine(app_config, tmp_path, kill=kill)
    engine.restore()
    engine.set_paused(False, reason="unit-test")
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.ingest_confirmed([c1])
    assert "BTCUSDT" in engine.positions
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
    assert "BTCUSDT" not in engine.positions
    assert any(call.get("reduceOnly") is True for call in http.place_calls)
    db.close()


def test_same_side_existing_position_skips_entry(app_config: AppConfig, tmp_path) -> None:
    http = FakeLiveHTTP()
    http.set_position("BTCUSDT", side="Buy", size="0.05", sl="80", avg="100")
    engine, db, _kill, http, _client = _engine(app_config, tmp_path, http)
    engine.restore()
    engine.set_paused(False, reason="unit-test")
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    before = len(http.place_calls)
    engine.ingest_confirmed([c1])
    assert len(http.place_calls) == before
    assert engine.positions["BTCUSDT"].size == Decimal("0.05")
    db.close()


def test_reverse_flattens_then_enters_opposite(app_config: AppConfig, tmp_path) -> None:
    http = FakeLiveHTTP()
    http.set_position("BTCUSDT", side="Buy", size="0.05", sl="80", avg="100")
    engine, db, _kill, http, _client = _engine(app_config, tmp_path, http, strategy=AlwaysShort())
    engine.restore()
    engine.set_paused(False, reason="unit-test")
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.ingest_confirmed([c1])
    assert any(call.get("reduceOnly") is True for call in http.place_calls)
    assert any(call.get("side") == "Sell" and call.get("reduceOnly") is not True for call in http.place_calls)
    assert "BTCUSDT" in engine.positions
    assert engine.positions["BTCUSDT"].side == "Sell"
    db.close()


def test_dry_run_does_not_place(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, _client = _engine(app_config, tmp_path, dry_run=True)
    engine.restore()
    engine.set_paused(False, reason="unit-test")
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.ingest_confirmed([c1])
    assert http.place_calls == []
    snap = engine.snapshot()
    assert snap["dry_run"] is True
    assert snap["real_orders"] is False
    assert "does not guarantee" in snap["disclaimer"].lower()
    db.close()


def test_private_ws_failure_does_not_pause_entries(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, _client = _engine(app_config, tmp_path)
    engine.restore()
    engine.set_paused(False, reason="public connected")
    engine.on_private_state(ConnectionState.FAILED, "auth failed")
    assert engine.paused is False
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.ingest_confirmed([c1])
    assert http.place_calls
    db.close()


def test_refresh_does_not_recheck_api_key(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, _client = _engine(app_config, tmp_path)
    engine.restore()
    assert http.api_key_calls == 1
    engine.set_paused(False, reason="unit-test")
    c0, c1 = _pair()
    engine.load_history([c0])
    engine.seed_if_fresh()
    engine.ingest_confirmed([c1])
    assert http.api_key_calls == 1
    db.close()


def test_set_leverage_110043_is_ok(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, client = _engine(app_config, tmp_path)
    http.leverage_unchanged = True
    apply_leverage(_testnet(app_config), client, ["BTCUSDT"], dry_run=False)
    assert http.leverage_calls
    apply_leverage(_testnet(app_config), client, ["BTCUSDT"], dry_run=True)
    db.close()


def test_disclaimer_mentions_testnet() -> None:
    assert "testnet" in TESTNET_DISCLAIMER.lower()
    assert "does not guarantee" in TESTNET_DISCLAIMER.lower()


class _FakeMarket:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles_data = candles

    def candles(self, symbol: str, timeframe: str, *, limit: int = 200, include_unclosed: bool = False):
        return list(self.candles_data[-limit:])


class _FakeWS:
    def __init__(self, config, kind=None, on_message=None, on_state=None):
        self.kind = kind
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


class _BrokenPrivateWS(_FakeWS):
    def start(self) -> None:
        raise ConnectionError("private ws refused")


def test_runner_places_only_on_live_ws_bar(app_config: AppConfig, tmp_path) -> None:
    engine, db, _kill, http, client = _engine(app_config, tmp_path)
    engine.restore()
    c0, c1 = _pair()
    live = Candle(
        symbol="BTCUSDT",
        interval="15",
        start_time=datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc),
        open=Decimal("111"),
        high=Decimal("112"),
        low=Decimal("110"),
        close=Decimal("111.5"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        confirmed=True,
    )
    market = _FakeMarket([c0, c1])
    ticks = {"t": 0.0}
    public_ws: dict = {}

    def time_fn() -> float:
        return ticks["t"]

    def sleep_fn(_seconds: float) -> None:
        ticks["t"] += 1.0
        ws = public_ws.get("ws")
        if ws and ws.on_message and ticks["t"] >= 1.0:
            ws.on_message(
                {
                    "topic": "kline.15.BTCUSDT",
                    "data": [
                        {
                            "start": live.start_ms,
                            "interval": "15",
                            "open": str(live.open),
                            "high": str(live.high),
                            "low": str(live.low),
                            "close": str(live.close),
                            "volume": "1",
                            "turnover": "1",
                            "confirm": True,
                        }
                    ],
                }
            )

    def factory(config, kind=None, on_message=None, on_state=None):
        ws = _FakeWS(config, kind=kind, on_message=on_message, on_state=on_state)
        if kind is None or getattr(kind, "value", kind) == "public":
            public_ws["ws"] = ws
        return ws

    snapshot = run_testnet_loop(
        app_config,
        engine,
        market=market,
        symbols=["BTCUSDT"],
        seconds=1.0,
        warmup=10,
        client=client,
        sleep_fn=sleep_fn,
        time_fn=time_fn,
        websocket_factory=factory,
        private_websocket_factory=_BrokenPrivateWS,
        enable_private_ws=True,
    )
    assert snapshot["real_orders"] is True
    assert http.place_calls
    assert "BTCUSDT" in engine.positions
    db.close()


def test_main_live_refuses_paper_mode(app_config: AppConfig) -> None:
    from trading_bot.main import main

    assert main(["live", "--seconds", "1"]) == 1
