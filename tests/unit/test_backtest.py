from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.backtest.engine import BacktestEngine
from trading_bot.backtest.runner import run_backtest_suite
from trading_bot.backtest.splits import funding_events_in_bar, split_ranges
from trading_bot.config.models import AppConfig
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import SignalType
from trading_bot.exchange.instruments import parse_instrument
from trading_bot.market.candles import Candle
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


def test_fill_uses_next_open_not_signal_close(app_config: AppConfig) -> None:
    app_config.execution.slippage = Decimal("0")
    app_config.execution.spread = Decimal("0")
    app_config.fees.taker = Decimal("0")
    app_config.fees.assumed_funding_rate = Decimal("0")
    app_config.risk.max_position_size = Decimal("1")
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
    engine = BacktestEngine(app_config, OneShotLong(), RiskManager(app_config, KillSwitch()), _instrument())
    result = engine.run([c0, c1])
    assert result.trades
    assert result.trades[0].entry_price == Decimal("110.0") or result.trades[0].entry_price == Decimal("110")
    assert result.trades[0].entry_price != c0.close


def test_stop_loss_and_costs_are_separated(app_config: AppConfig) -> None:
    app_config.execution.slippage = Decimal("0.001")
    app_config.fees.taker = Decimal("0.0005")
    app_config.fees.assumed_funding_rate = Decimal("0")
    app_config.risk.max_position_size = Decimal("1")
    candles = make_candles([100, 100, 50])
    # Force a wide range on the last bar so the stop is hit.
    last = candles[-1]
    candles[-1] = Candle(
        symbol=last.symbol,
        interval=last.interval,
        start_time=last.start_time,
        open=last.open,
        high=last.high,
        low=Decimal("1"),
        close=Decimal("40"),
        volume=last.volume,
        turnover=last.turnover,
        confirmed=True,
    )
    engine = BacktestEngine(app_config, OneShotLong(), RiskManager(app_config, KillSwitch()), _instrument())
    result = engine.run(candles)
    assert result.trades
    trade = result.trades[0]
    assert trade.fees and trade.fees > 0
    assert trade.slippage and trade.slippage > 0
    assert trade.net_pnl is not None
    assert trade.gross_pnl is not None
    assert trade.net_pnl == trade.gross_pnl - trade.fees - (trade.funding or 0) - trade.slippage


def test_oos_split_sizes() -> None:
    ranges = split_ranges(100, Decimal("0.6"), Decimal("0.2"), Decimal("0.2"))
    assert [r.name for r in ranges] == ["development", "validation", "out_of_sample"]
    assert ranges[0].end == 60
    assert ranges[1].end == 80
    assert ranges[2].end == 100


def test_suite_returns_disclaimer_and_splits(app_config: AppConfig) -> None:
    app_config.execution.slippage = Decimal("0")
    app_config.fees.assumed_funding_rate = Decimal("0")
    candles = make_candles([100 + (i % 7) for i in range(120)])
    report = run_backtest_suite(app_config, candles, instrument=_instrument())
    assert "does not guarantee" in report["disclaimer"].lower() or "does not guarantee" in report["full"]["disclaimer"].lower()
    assert "development" in report["splits"]
    assert "out_of_sample" in report["splits"]
    assert "walk_forward" in report


def test_funding_events_count() -> None:
    # 8h funding, 15m bar at 07:45 UTC contains no funding; 08:00 bar contains one.
    start_0745 = int(datetime(2024, 1, 1, 7, 45, tzinfo=timezone.utc).timestamp() * 1000)
    start_0800 = int(datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert funding_events_in_bar(start_0745, 900_000, 8) == 0
    assert funding_events_in_bar(start_0800, 900_000, 8) == 1
