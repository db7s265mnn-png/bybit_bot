from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any

from trading_bot.core.exceptions import InvalidOrderError


def to_decimal(value: Any, default: str | None = None) -> Decimal:
    if value is None or value == "":
        if default is None:
            raise InvalidOperation("empty decimal")
        value = default
    return Decimal(str(value))


def optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


@dataclass(frozen=True)
class Instrument:
    """Trading constraints from GET /v5/market/instruments-info. Never hardcode these."""

    category: str
    symbol: str
    status: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    contract_type: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    max_mkt_order_qty: Decimal | None
    min_notional: Decimal | None
    min_leverage: Decimal | None
    max_leverage: Decimal | None
    leverage_step: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None
    funding_interval_min: int | None
    raw: dict[str, Any]

    @property
    def is_trading(self) -> bool:
        return self.status == "Trading"

    def round_price(self, price: Decimal, *, side: str | None = None) -> Decimal:
        """Round to tick size. BUY limits round down (never more aggressive), SELL up."""
        rounding = ROUND_DOWN if (side or "").lower() in {"buy", "long"} else ROUND_UP
        if (side or "").lower() in {"sell", "short"}:
            rounding = ROUND_UP
        if side is None:
            rounding = ROUND_DOWN
        ticks = (price / self.tick_size).to_integral_value(rounding=rounding)
        return (ticks * self.tick_size).quantize(self.tick_size)

    def round_qty(self, qty: Decimal) -> Decimal:
        steps = (qty / self.qty_step).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.qty_step).quantize(self.qty_step)

    def validate_qty(self, qty: Decimal, *, market: bool = False) -> Decimal:
        rounded = self.round_qty(qty)
        if rounded < self.min_order_qty:
            raise InvalidOrderError(
                f"{self.symbol} qty {rounded} below minOrderQty {self.min_order_qty}",
                ret_code=None,
            )
        cap = self.max_mkt_order_qty if market and self.max_mkt_order_qty is not None else self.max_order_qty
        if rounded > cap:
            raise InvalidOrderError(
                f"{self.symbol} qty {rounded} above max order qty {cap}",
                ret_code=None,
            )
        return rounded

    def validate_notional(self, qty: Decimal, price: Decimal) -> None:
        if self.min_notional is None:
            return
        notional = qty * price
        if notional < self.min_notional:
            raise InvalidOrderError(
                f"{self.symbol} notional {notional} below minNotionalValue {self.min_notional}",
                ret_code=None,
            )


def parse_instrument(category: str, item: dict[str, Any]) -> Instrument:
    price_filter = item.get("priceFilter") or {}
    lot_filter = item.get("lotSizeFilter") or {}
    leverage_filter = item.get("leverageFilter") or {}
    return Instrument(
        category=category,
        symbol=str(item["symbol"]),
        status=str(item.get("status", "")),
        base_coin=str(item.get("baseCoin", "")),
        quote_coin=str(item.get("quoteCoin", "")),
        settle_coin=str(item.get("settleCoin", item.get("quoteCoin", ""))),
        contract_type=str(item.get("contractType", "")),
        tick_size=to_decimal(price_filter.get("tickSize") or lot_filter.get("tickSize") or "0.01"),
        qty_step=to_decimal(lot_filter.get("qtyStep") or lot_filter.get("basePrecision") or "0.001"),
        min_order_qty=to_decimal(lot_filter.get("minOrderQty") or "0"),
        max_order_qty=to_decimal(lot_filter.get("maxOrderQty") or lot_filter.get("maxLimitOrderQty") or "0"),
        max_mkt_order_qty=optional_decimal(
            lot_filter.get("maxMktOrderQty") or lot_filter.get("maxMarketOrderQty")
        ),
        min_notional=optional_decimal(
            lot_filter.get("minNotionalValue") or lot_filter.get("minOrderAmt")
        ),
        min_leverage=optional_decimal(leverage_filter.get("minLeverage")),
        max_leverage=optional_decimal(leverage_filter.get("maxLeverage")),
        leverage_step=optional_decimal(leverage_filter.get("leverageStep")),
        min_price=optional_decimal(price_filter.get("minPrice")),
        max_price=optional_decimal(price_filter.get("maxPrice")),
        funding_interval_min=int(item["fundingInterval"]) if item.get("fundingInterval") else None,
        raw=item,
    )
