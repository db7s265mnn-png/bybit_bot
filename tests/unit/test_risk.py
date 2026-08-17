from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.config.models import AppConfig, RiskConfig
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import SignalType
from trading_bot.exchange.instruments import parse_instrument
from trading_bot.risk.position_sizing import loss_per_unit, size_from_risk
from trading_bot.risk.risk_manager import OpenRisk, RiskManager
from trading_bot.strategy.base import Signal


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


def _signal(stop: Decimal) -> Signal:
    return Signal(
        type=SignalType.LONG,
        symbol="BTCUSDT",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        stop_loss=stop,
        take_profit=Decimal("104"),
        confidence=Decimal("1"),
    )


def test_size_matches_risk_over_stop_distance() -> None:
    # $10,000, 0.5% risk = $50. 2% stop on $100,000 → $2,000 per BTC → 0.025 BTC.
    loss = loss_per_unit(
        SignalType.LONG,
        Decimal("100000"),
        Decimal("98000"),
        taker=Decimal("0"),
        slippage=Decimal("0"),
    )
    qty, risk_amount = size_from_risk(Decimal("10000"), Decimal("0.005"), loss)
    assert risk_amount == Decimal("50")
    assert qty == Decimal("0.025")


def test_fees_and_slippage_reduce_quantity() -> None:
    naked = loss_per_unit(
        SignalType.LONG, Decimal("100"), Decimal("98"), taker=Decimal("0"), slippage=Decimal("0")
    )
    loaded = loss_per_unit(
        SignalType.LONG,
        Decimal("100"),
        Decimal("98"),
        taker=Decimal("0.001"),
        slippage=Decimal("0.001"),
    )
    assert loaded > naked
    qty_naked, _ = size_from_risk(Decimal("10000"), Decimal("0.005"), naked)
    qty_loaded, _ = size_from_risk(Decimal("10000"), Decimal("0.005"), loaded)
    assert qty_loaded < qty_naked


def test_risk_manager_caps_notional_and_portfolio(app_config) -> None:
    app_config.risk = RiskConfig(
        risk_per_trade=Decimal("0.005"),
        max_daily_loss=Decimal("0.02"),
        max_open_positions=1,
        max_position_size=Decimal("0.25"),
        max_portfolio_risk=Decimal("0.006"),
        max_consecutive_losses=5,
        max_leverage=Decimal("5"),
    )
    manager = RiskManager(app_config)
    decision = manager.evaluate(
        equity=Decimal("10000"),
        signal=_signal(Decimal("98")),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        instrument=_instrument(),
        open_positions=[],
        at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert decision.allowed
    assert decision.notional <= Decimal("2500")

    blocked = manager.evaluate(
        equity=Decimal("10000"),
        signal=_signal(Decimal("98")),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        instrument=_instrument(),
        open_positions=[OpenRisk("ETHUSDT", Decimal("50"))],
        at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert blocked.allowed is False
    assert "max open positions" in blocked.reason


def test_daily_loss_and_kill_switch_halt(app_config) -> None:
    switch = KillSwitch()
    manager = RiskManager(app_config, switch)
    manager.note_session_equity(Decimal("10000"), datetime(2024, 1, 1, tzinfo=timezone.utc))
    manager.record_closed_trade(Decimal("-250"))
    halted = manager.evaluate(
        equity=Decimal("9750"),
        signal=_signal(Decimal("98")),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        instrument=_instrument(),
        open_positions=[],
        at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert halted.allowed is False
    assert "daily loss" in halted.reason

    manager2 = RiskManager(app_config, switch)
    switch.activate("test")
    killed = manager2.evaluate(
        equity=Decimal("10000"),
        signal=_signal(Decimal("98")),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        instrument=_instrument(),
        open_positions=[],
        at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert killed.allowed is False
