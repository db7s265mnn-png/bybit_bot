from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.config.models import AppConfig, TradingMode
from trading_bot.core.exceptions import (
    DuplicateClientOrderError,
    KillSwitchActiveError,
    LiveOrdersBlockedError,
    OrderNotFilledError,
    StopLossMissingError,
)
from trading_bot.core.ids import new_order_link_id
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.retry import is_retryable
from trading_bot.database.database import Database
from trading_bot.exchange.bybit_client import BybitRESTClient, _error_from_code
from trading_bot.exchange.instruments import parse_instrument
from trading_bot.execution.order_manager import OrderManager
from trading_bot.execution.order_state import OrderIntent, parse_exchange_order, parse_ws_order_message


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeOrderHTTP:
    def __init__(self) -> None:
        self.place_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.stop_calls: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.status_queue: dict[str, list[str]] = {}
        self.position_sl = "90000"
        self.position_size = "0.01"
        self.position_side = "Buy"
        self.duplicate = False
        self.seq = 0
        self.apply_sl_on_stop = True

    def place_order(self, **kwargs) -> dict:
        self.place_calls.append(kwargs)
        if self.duplicate:
            return {"retCode": 110072, "retMsg": "Duplicate orderLinkId"}
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
                row["avgPrice"] = "100000"
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
        if self.apply_sl_on_stop and kwargs.get("stopLoss"):
            self.position_sl = str(kwargs["stopLoss"])
        return {"retCode": 0, "result": {}}

    def get_positions(self, **kwargs) -> dict:
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": kwargs.get("symbol") or "BTCUSDT",
                        "side": self.position_side,
                        "size": self.position_size,
                        "avgPrice": "100000",
                        "stopLoss": self.position_sl,
                        "takeProfit": "",
                        "positionIdx": 0,
                    }
                ]
            },
        }


def _testnet(app_config: AppConfig) -> AppConfig:
    app_config.system.mode = TradingMode.TESTNET
    app_config.exchange.testnet = True
    app_config.secrets.bybit_api_key = "test-key"
    app_config.secrets.bybit_api_secret = "test-secret"
    app_config.orders.fill_timeout_sec = 5.0
    app_config.orders.poll_interval_sec = 0.5
    app_config.orders.sl_confirm_attempts = 3
    app_config.orders.sl_confirm_delay_sec = 0.5
    return app_config


def _manager(app_config: AppConfig, http: FakeOrderHTTP, tmp_path, kill=None):
    clock = Clock()
    db = Database(f"sqlite:///{tmp_path / 'orders.db'}")
    client = BybitRESTClient(_testnet(app_config), http=http)
    manager = OrderManager(
        app_config,
        client,
        database=db,
        kill_switch=kill or KillSwitch(),
        session_id="om-test",
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    return manager, clock, db, client


def _intent(link: str, *, reduce_only: bool = False) -> OrderIntent:
    return OrderIntent(
        symbol="BTCUSDT",
        side="Buy" if not reduce_only else "Sell",
        order_type="Market",
        quantity=Decimal("0.01"),
        client_order_id=link,
        reduce_only=reduce_only,
        stop_loss=None if reduce_only else Decimal("90000"),
        take_profit=None if reduce_only else Decimal("120000"),
    )


def test_created_is_not_a_fill(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    manager, _clock, db, _client = _manager(app_config, http, tmp_path)
    instrument = parse_instrument("linear", btc_instrument_payload)
    filled = manager.submit(_intent("idemp001"), instrument=instrument)
    assert filled.status == "Filled"
    assert filled.filled_qty == Decimal("0.01")
    assert http.place_calls[0]["orderLinkId"] == "idemp001"
    assert Decimal(http.place_calls[0].get("stopLoss")) == Decimal("90000")
    stored = db.get_order_by_client_id("idemp001")
    assert stored is not None
    assert stored.status == "Filled"
    db.close()


def test_duplicate_order_link_id_fetches_existing(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    http.duplicate = True
    http.orders["dup-1"] = {
        "orderId": "ex-9",
        "orderLinkId": "dup-1",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.01",
        "orderStatus": "Filled",
        "cumExecQty": "0.01",
        "avgPrice": "100000",
        "reduceOnly": False,
        "stopLoss": "90000",
        "takeProfit": "",
        "rejectReason": "",
    }
    http.status_queue["dup-1"] = []
    manager, _clock, db, _client = _manager(app_config, http, tmp_path)
    filled = manager.submit(_intent("dup-1"), instrument=parse_instrument("linear", btc_instrument_payload))
    assert filled.status == "Filled"
    assert filled.order_id == "ex-9"
    db.close()


def test_paper_mode_never_places(app_config, tmp_path) -> None:
    http = FakeOrderHTTP()
    clock = Clock()
    db = Database(f"sqlite:///{tmp_path / 'p.db'}")
    app_config.secrets.bybit_api_key = "k"
    app_config.secrets.bybit_api_secret = "s"
    client = BybitRESTClient(app_config, http=http)
    manager = OrderManager(app_config, client, database=db, sleep_fn=clock.sleep, time_fn=clock.time)
    with pytest.raises(LiveOrdersBlockedError):
        manager.submit(_intent("paper-1"))
    assert http.place_calls == []
    db.close()


def test_kill_switch_blocks_entries_allows_flatten(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    kill = KillSwitch()
    kill.activate("unit-test")
    manager, _clock, db, _client = _manager(app_config, http, tmp_path, kill=kill)
    instrument = parse_instrument("linear", btc_instrument_payload)
    with pytest.raises(KillSwitchActiveError):
        manager.submit(_intent("kill-1"), instrument=instrument)
    assert http.place_calls == []
    closed = manager.flatten_symbol("BTCUSDT", instrument=instrument)
    assert closed is not None
    assert closed.reduce_only is True
    assert http.place_calls[0]["reduceOnly"] is True
    db.close()


def test_mainnet_without_confirm_blocks_entry_allows_protect(app_config) -> None:
    app_config.system.mode = TradingMode.MAINNET
    app_config.exchange.testnet = False
    app_config.system.live_trading_confirm = False
    app_config.secrets.bybit_api_key = "k"
    app_config.secrets.bybit_api_secret = "s"
    assert app_config.mutating_orders_allowed() is True
    assert app_config.live_orders_allowed() is False
    http = FakeOrderHTTP()
    clock = Clock()
    client = BybitRESTClient(app_config, http=http)
    manager = OrderManager(app_config, client, sleep_fn=clock.sleep, time_fn=clock.time)
    with pytest.raises(LiveOrdersBlockedError):
        manager.submit(_intent("mn-1"))
    assert http.place_calls == []


def test_missing_sl_is_critical_and_flattens(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    http.position_sl = ""
    http.apply_sl_on_stop = False
    manager, _clock, db, _client = _manager(app_config, http, tmp_path)
    with pytest.raises(StopLossMissingError):
        manager.submit(_intent("sl-miss"), instrument=parse_instrument("linear", btc_instrument_payload))
    assert http.stop_calls
    assert any(call.get("reduceOnly") is True for call in http.place_calls)
    db.close()


def test_partial_fill_timeout_does_not_resubmit(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    http.status_queue["part-1"] = ["PartiallyFilled", "PartiallyFilled", "PartiallyFilled", "PartiallyFilled"]
    manager, _clock, db, _client = _manager(app_config, http, tmp_path)
    result = manager.submit(_intent("part-1"), instrument=parse_instrument("linear", btc_instrument_payload))
    assert result.status == "PartiallyFilled"
    assert result.filled_qty > 0
    assert len(http.place_calls) == 1
    db.close()


def test_ws_fill_is_accepted(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    http.status_queue["ws-1"] = ["New", "New", "New", "New", "New"]
    manager, clock, db, _client = _manager(app_config, http, tmp_path)

    original_sleep = clock.sleep

    def sleep_then_ws(seconds: float) -> None:
        original_sleep(seconds)
        manager.apply_ws_message(
            {
                "topic": "order",
                "data": [
                    {
                        "orderId": "ex-ws",
                        "orderLinkId": "ws-1",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "orderType": "Market",
                        "qty": "0.01",
                        "orderStatus": "Filled",
                        "cumExecQty": "0.01",
                        "avgPrice": "100000",
                        "reduceOnly": False,
                    }
                ],
            }
        )

    manager._sleep = sleep_then_ws
    filled = manager.submit(_intent("ws-1"), instrument=parse_instrument("linear", btc_instrument_payload))
    assert filled.status == "Filled"
    db.close()


def test_idempotent_replay_skips_second_place(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    manager, _clock, db, _client = _manager(app_config, http, tmp_path)
    instrument = parse_instrument("linear", btc_instrument_payload)
    manager.submit(_intent("once-1"), instrument=instrument)
    http2 = FakeOrderHTTP()
    client2 = BybitRESTClient(app_config, http=http2)
    clock = Clock()
    manager2 = OrderManager(
        app_config, client2, database=db, sleep_fn=clock.sleep, time_fn=clock.time, session_id="om-test"
    )
    again = manager2.submit(_intent("once-1"), instrument=instrument)
    assert again.status == "Filled"
    assert http2.place_calls == []
    db.close()


def test_unfilled_working_order_is_cancelled(app_config, btc_instrument_payload, tmp_path) -> None:
    http = FakeOrderHTTP()
    http.status_queue["hang-1"] = ["New", "New", "New", "New", "New", "New", "New", "New", "New", "New"]
    app_config.orders.fill_timeout_sec = 1.0
    manager, _clock, db, _client = _manager(app_config, http, tmp_path)
    with pytest.raises(OrderNotFilledError):
        manager.submit(_intent("hang-1"), instrument=parse_instrument("linear", btc_instrument_payload))
    assert http.cancel_calls
    db.close()


def test_ws_parser_ignores_other_topics() -> None:
    assert parse_ws_order_message({"topic": "position", "data": [{}]}) == []
    orders = parse_ws_order_message(
        {"topic": "order", "data": [{"orderLinkId": "x", "orderStatus": "New", "symbol": "ETHUSDT"}]}
    )
    assert orders[0].symbol == "ETHUSDT"


def test_order_link_id_fits_bybit_limit() -> None:
    link = new_order_link_id("testnet")
    assert len(link) <= 36
    assert link.isalnum()


def test_duplicate_ret_code_maps() -> None:
    err = _error_from_code("dup", ret_code=110072)
    assert isinstance(err, DuplicateClientOrderError)
    assert not is_retryable(err)


def test_created_status_is_not_confirmed_fill() -> None:
    order = parse_exchange_order({"orderStatus": "Created", "orderLinkId": "a", "qty": "1"})
    assert order.is_confirmed_fill() is False
    assert order.is_working() is True
