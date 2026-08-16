from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.database.database import Database, OrderRecord, TradeRecord
from trading_bot.market.candles import Candle


def test_sqlite_roundtrip_candles_trades_orders_events(tmp_path) -> None:
    db = Database(f"sqlite:///{tmp_path / 't.db'}")
    candle = Candle(
        symbol="BTCUSDT",
        interval="15",
        start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1"),
        turnover=Decimal("100"),
        confirmed=True,
    )
    db.upsert_candles([candle])
    loaded = db.load_candles("BTCUSDT", "15")
    assert len(loaded) == 1
    assert loaded[0].close == Decimal("105")

    trade = TradeRecord(
        trade_id="t1",
        symbol="BTCUSDT",
        side="Buy",
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        quantity=Decimal("0.01"),
        entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        exit_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
        gross_pnl=Decimal("0.1"),
        fees=Decimal("0.01"),
        funding=Decimal("0"),
        slippage=Decimal("0.001"),
        net_pnl=Decimal("0.089"),
        strategy="ema_crossover",
    )
    db.upsert_trade(trade)
    assert db.list_trades()[0].net_pnl == Decimal("0.089")

    order = OrderRecord(
        order_id="o1",
        client_order_id="c1",
        symbol="BTCUSDT",
        side="Buy",
        type="Market",
        price=Decimal("100"),
        quantity=Decimal("0.01"),
        status="Filled",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    db.upsert_order(order)
    assert db.get_order_by_client_id("c1") is not None

    event = db.record_event("test", "hello", level="INFO")
    assert event.event_id
    assert db.list_events()[0].message == "hello"
    db.close()
