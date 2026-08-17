from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_bot.exchange.instruments import optional_decimal, to_decimal


@dataclass(frozen=True)
class PriceLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    ts_ms: int
    update_id: int | None
    seq: int | None

    @property
    def best_bid(self) -> PriceLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> PriceLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if not self.best_bid or not self.best_ask:
            return None
        return (self.best_bid.price + self.best_ask.price) / Decimal("2")

    @property
    def spread(self) -> Decimal | None:
        if not self.best_bid or not self.best_ask:
            return None
        return self.best_ask.price - self.best_bid.price

    @property
    def spread_pct(self) -> Decimal | None:
        mid = self.mid
        spread = self.spread
        if mid is None or spread is None or mid == 0:
            return None
        return spread / mid


def parse_orderbook(payload: dict[str, Any]) -> OrderBook:
    result = payload.get("result") or payload.get("data") or payload
    bids = tuple(PriceLevel(to_decimal(level[0]), to_decimal(level[1])) for level in result.get("b") or [])
    asks = tuple(PriceLevel(to_decimal(level[0]), to_decimal(level[1])) for level in result.get("a") or [])
    return OrderBook(
        symbol=str(result.get("s") or result.get("symbol") or ""),
        bids=bids,
        asks=asks,
        ts_ms=int(result.get("ts") or payload.get("ts") or 0),
        update_id=int(result["u"]) if result.get("u") is not None else None,
        seq=int(result["seq"]) if result.get("seq") is not None else None,
    )


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last_price: Decimal
    mark_price: Decimal | None
    index_price: Decimal | None
    bid1: Decimal | None
    ask1: Decimal | None
    bid1_size: Decimal | None
    ask1_size: Decimal | None
    funding_rate: Decimal | None
    next_funding_time_ms: int | None
    turnover_24h: Decimal | None
    volume_24h: Decimal | None

    @property
    def spread(self) -> Decimal | None:
        if self.bid1 is None or self.ask1 is None:
            return None
        return self.ask1 - self.bid1


def parse_ticker(item: dict[str, Any]) -> Ticker:
    next_funding = item.get("nextFundingTime")
    return Ticker(
        symbol=str(item.get("symbol") or ""),
        last_price=to_decimal(item.get("lastPrice")),
        mark_price=optional_decimal(item.get("markPrice")),
        index_price=optional_decimal(item.get("indexPrice")),
        bid1=optional_decimal(item.get("bid1Price")),
        ask1=optional_decimal(item.get("ask1Price")),
        bid1_size=optional_decimal(item.get("bid1Size")),
        ask1_size=optional_decimal(item.get("ask1Size")),
        funding_rate=optional_decimal(item.get("fundingRate")),
        next_funding_time_ms=int(next_funding) if next_funding else None,
        turnover_24h=optional_decimal(item.get("turnover24h")),
        volume_24h=optional_decimal(item.get("volume24h")),
    )
