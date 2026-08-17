from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.core.exceptions import InvalidOrderError
from trading_bot.exchange.instruments import parse_instrument


def test_parse_and_round_from_api_payload(btc_instrument_payload: dict) -> None:
    instrument = parse_instrument("linear", btc_instrument_payload)
    assert instrument.tick_size == Decimal("0.10")
    assert instrument.qty_step == Decimal("0.001")
    assert instrument.min_order_qty == Decimal("0.001")
    assert instrument.min_notional == Decimal("5")
    assert instrument.round_qty(Decimal("0.0019")) == Decimal("0.001")
    assert instrument.round_price(Decimal("100000.17"), side="Buy") == Decimal("100000.10")
    assert instrument.round_price(Decimal("100000.11"), side="Sell") == Decimal("100000.20")


def test_qty_below_minimum_rejected(btc_instrument_payload: dict) -> None:
    instrument = parse_instrument("linear", btc_instrument_payload)
    with pytest.raises(InvalidOrderError, match="minOrderQty"):
        instrument.validate_qty(Decimal("0.0001"))


def test_notional_rejected(btc_instrument_payload: dict) -> None:
    instrument = parse_instrument("linear", btc_instrument_payload)
    with pytest.raises(InvalidOrderError, match="minNotionalValue"):
        instrument.validate_notional(Decimal("0.001"), Decimal("100"))
