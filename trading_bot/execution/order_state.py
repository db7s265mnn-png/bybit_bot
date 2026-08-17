from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trading_bot.exchange.instruments import optional_decimal, to_decimal

FILLED_STATUSES = frozenset({"Filled"})
PARTIAL_STATUSES = frozenset({"PartiallyFilled", "PartiallyFilledCanceled"})
DEAD_STATUSES = frozenset({"Cancelled", "Canceled", "Rejected", "Deactivated", "PartiallyFilledCanceled"})
WORKING_STATUSES = frozenset(
    {"Created", "New", "Untriggered", "Triggered", "Active", "PartiallyFilled"}
)
# Created / New / Untriggered are not fills.
UNCONFIRMED_STATUSES = frozenset({"Created", "New", "Untriggered", "Triggered", "Active"})


def decimal_str(value: Decimal) -> str:
    return format(value, "f")


@dataclass
class ExchangeOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    price: Decimal | None
    status: str
    filled_qty: Decimal
    avg_price: Decimal | None
    leaves_qty: Decimal | None
    reduce_only: bool
    reject_reason: str
    stop_loss: str
    take_profit: str
    time_in_force: str
    raw: dict[str, Any] = field(default_factory=dict)

    def is_filled(self) -> bool:
        return self.status in FILLED_STATUSES

    def is_working(self) -> bool:
        return self.status in WORKING_STATUSES

    def is_dead(self) -> bool:
        return self.status in DEAD_STATUSES

    def is_terminal(self) -> bool:
        return self.is_filled() or self.is_dead()

    def is_confirmed_fill(self) -> bool:
        """HTTP 200 / Created is not a fill. Require Filled (or a recorded partial)."""
        return self.is_filled() or (
            self.status == "PartiallyFilledCanceled" and self.filled_qty > 0
        )


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    client_order_id: str
    price: Decimal | None = None
    reduce_only: bool = False
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    time_in_force: str | None = None
    position_idx: int | None = None


def parse_exchange_order(item: dict[str, Any], *, fallback_symbol: str = "") -> ExchangeOrder:
    qty = to_decimal(item.get("qty") or item.get("orderQty") or "0")
    filled = to_decimal(item.get("cumExecQty") or item.get("cumExecQty") or "0")
    leaves = optional_decimal(item.get("leavesQty"))
    return ExchangeOrder(
        order_id=str(item.get("orderId") or ""),
        client_order_id=str(item.get("orderLinkId") or ""),
        symbol=str(item.get("symbol") or fallback_symbol),
        side=str(item.get("side") or ""),
        order_type=str(item.get("orderType") or ""),
        qty=qty,
        price=optional_decimal(item.get("price")),
        status=str(item.get("orderStatus") or item.get("status") or ""),
        filled_qty=filled,
        avg_price=optional_decimal(item.get("avgPrice")),
        leaves_qty=leaves,
        reduce_only=bool(item.get("reduceOnly")),
        reject_reason=str(item.get("rejectReason") or ""),
        stop_loss=str(item.get("stopLoss") or ""),
        take_profit=str(item.get("takeProfit") or ""),
        time_in_force=str(item.get("timeInForce") or ""),
        raw=dict(item),
    )


def parse_ws_order_message(payload: dict[str, Any]) -> list[ExchangeOrder]:
    topic = str(payload.get("topic") or "")
    if topic != "order" and not topic.startswith("order."):
        return []
    rows = payload.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    return [parse_exchange_order(item) for item in rows]


def sl_matches(position_sl: str, expected: Decimal, *, tick: Decimal | None = None) -> bool:
    if not position_sl or position_sl in {"0", "0.0"}:
        return False
    actual = to_decimal(position_sl)
    if actual <= 0:
        return False
    if tick is not None and tick > 0:
        return abs(actual - expected) <= tick
    return abs(actual - expected) / expected <= Decimal("0.0001") if expected else actual == expected


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
