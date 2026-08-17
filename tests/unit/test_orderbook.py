from __future__ import annotations

from decimal import Decimal

from trading_bot.market.orderbook import parse_orderbook, parse_ticker


def test_orderbook_spread() -> None:
    payload = {
        "result": {
            "s": "BTCUSDT",
            "b": [["100.0", "2"], ["99.5", "1"]],
            "a": [["100.2", "3"], ["100.4", "4"]],
            "ts": 1,
            "u": 2,
            "seq": 3,
        }
    }
    book = parse_orderbook(payload)
    assert book.best_bid.price == Decimal("100.0")
    assert book.best_ask.price == Decimal("100.2")
    assert book.spread == Decimal("0.2")
    assert book.mid == Decimal("100.1")


def test_ticker_spread() -> None:
    ticker = parse_ticker(
        {
            "symbol": "ETHUSDT",
            "lastPrice": "2000",
            "bid1Price": "1999.5",
            "ask1Price": "2000.5",
            "fundingRate": "0.0001",
        }
    )
    assert ticker.spread == Decimal("1.0")
    assert ticker.funding_rate == Decimal("0.0001")
