from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from threading import RLock
from typing import Any

from trading_bot.account.service import _parse_positions
from trading_bot.config.models import AppConfig
from trading_bot.core.exceptions import (
    DuplicateClientOrderError,
    InvalidOrderError,
    LiveOrdersBlockedError,
    OrderNotFilledError,
    StopLossMissingError,
)
from trading_bot.core.ids import new_order_link_id
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import Side
from trading_bot.database.database import Database, OrderRecord
from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.exchange.instruments import Instrument
from trading_bot.execution.order_state import (
    DEAD_STATUSES,
    ExchangeOrder,
    FILLED_STATUSES,
    OrderIntent,
    decimal_str,
    parse_exchange_order,
    parse_ws_order_message,
    sl_matches,
    utc_now,
)
from trading_bot.monitoring.logger import get_logger

logger = get_logger("trading_bot.order_manager")

ZERO = Decimal("0")


class OrderManager:
    """Idempotent live/testnet orders. HTTP 200 is not a fill; SL must be confirmed on the exchange.

    Paper/backtest must not call submit() — SimulatedBroker handles virtual fills.
    """

    def __init__(
        self,
        config: AppConfig,
        client: BybitRESTClient,
        *,
        database: Database | None = None,
        kill_switch: KillSwitch | None = None,
        session_id: str = "live",
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._client = client
        self._db = database
        self._kill = kill_switch
        self.session_id = session_id
        self._sleep = sleep_fn
        self._time = time_fn
        self._lock = RLock()
        self._by_link: dict[str, ExchangeOrder] = {}

    def make_client_id(self, prefix: str = "b") -> str:
        return new_order_link_id(prefix)

    def apply_ws_message(self, payload: dict[str, Any]) -> list[ExchangeOrder]:
        orders = parse_ws_order_message(payload)
        for order in orders:
            self._remember(order)
        return orders

    def submit(self, intent: OrderIntent, *, instrument: Instrument | None = None) -> ExchangeOrder:
        """Place if needed, then wait until the exchange reports a terminal status."""
        self._assert_may_submit(intent)
        qty = intent.quantity
        price = intent.price
        stop = intent.stop_loss
        take = intent.take_profit
        if instrument is not None:
            qty = instrument.validate_qty(qty, market=intent.order_type.lower() == "market")
            if price is not None:
                price = instrument.round_price(price, side=intent.side)
            if stop is not None:
                stop = instrument.round_price(stop, side="Sell" if intent.side.lower() == "buy" else "Buy")
            if take is not None:
                take = instrument.round_price(take, side=intent.side)
        if not intent.reduce_only and stop is None:
            raise InvalidOrderError("entry orders require a stop-loss to attach on the exchange")

        link_id = intent.client_order_id
        existing = self._existing(link_id)
        if existing is not None and existing.status in FILLED_STATUSES:
            logger.info("order_idempotent_filled", order_link_id=link_id, status=existing.status)
            return existing
        if existing is not None and existing.status in DEAD_STATUSES and existing.filled_qty <= 0:
            raise InvalidOrderError(f"orderLinkId {link_id} already used ({existing.status})")

        if existing is None or existing.status in {"", "Created"}:
            if self._db_terminal_filled(link_id):
                fetched = self._fetch(intent.symbol, link_id)
                if fetched and fetched.is_confirmed_fill():
                    return fetched
            try:
                raw = self._client.place_order(
                    symbol=intent.symbol,
                    side=intent.side,
                    order_type=intent.order_type,
                    qty=decimal_str(qty),
                    order_link_id=link_id,
                    price=None if price is None else decimal_str(price),
                    time_in_force=intent.time_in_force or self._tif(intent.order_type),
                    reduce_only=intent.reduce_only,
                    stop_loss=None if stop is None else decimal_str(stop),
                    take_profit=None if take is None else decimal_str(take),
                    position_idx=intent.position_idx if intent.position_idx is not None else self._config.orders.position_idx,
                    tpsl_mode=self._config.orders.tpsl_mode if stop or take else None,
                )
                result = (raw.get("result") or raw) if isinstance(raw, dict) else {}
                hinted = parse_exchange_order(
                    {
                        "orderId": result.get("orderId") or "",
                        "orderLinkId": result.get("orderLinkId") or link_id,
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "orderType": intent.order_type,
                        "qty": decimal_str(qty),
                        "price": None if price is None else decimal_str(price),
                        "orderStatus": result.get("orderStatus") or "Created",
                        "reduceOnly": intent.reduce_only,
                        "stopLoss": "" if stop is None else decimal_str(stop),
                        "takeProfit": "" if take is None else decimal_str(take),
                    },
                    fallback_symbol=intent.symbol,
                )
                self._remember(hinted)
                logger.info(
                    "order_accepted",
                    order_link_id=link_id,
                    order_id=hinted.order_id,
                    status=hinted.status,
                    note="Created/New is not a fill",
                )
            except DuplicateClientOrderError:
                logger.warning("order_link_id_duplicate", order_link_id=link_id)

        confirmed = self.wait_fill(intent.symbol, link_id)
        if not intent.reduce_only:
            self.ensure_protective_stops(
                intent.symbol,
                stop_loss=stop,
                take_profit=take,
                instrument=instrument,
            )
        return confirmed

    def wait_fill(self, symbol: str, order_link_id: str) -> ExchangeOrder:
        cfg = self._config.orders
        deadline = self._time() + cfg.fill_timeout_sec
        last: ExchangeOrder | None = None
        while self._time() <= deadline:
            cached = self._cached(order_link_id)
            if cached is not None and (
                cached.is_confirmed_fill() or (cached.is_dead() and cached.filled_qty <= 0)
            ):
                last = cached
            else:
                last = self._fetch(symbol, order_link_id) or cached
            if last is not None:
                self._remember(last)
                if last.is_confirmed_fill():
                    logger.info(
                        "order_filled",
                        order_link_id=order_link_id,
                        order_id=last.order_id,
                        status=last.status,
                        filled_qty=str(last.filled_qty),
                        avg_price=None if last.avg_price is None else str(last.avg_price),
                    )
                    return last
                if last.is_dead() and last.filled_qty <= 0:
                    raise OrderNotFilledError(
                        f"order {order_link_id} ended {last.status}"
                        + (f" ({last.reject_reason})" if last.reject_reason else "")
                    )
                if last.status == "PartiallyFilledCanceled" and last.filled_qty > 0:
                    logger.error(
                        "order_partial_terminal",
                        order_link_id=order_link_id,
                        filled_qty=str(last.filled_qty),
                        qty=str(last.qty),
                    )
                    return last
            self._sleep(cfg.poll_interval_sec)
        if last is not None and last.status == "PartiallyFilled" and last.filled_qty > 0:
            logger.error(
                "order_partial_timeout",
                order_link_id=order_link_id,
                filled_qty=str(last.filled_qty),
                qty=str(last.qty),
            )
            return last
        if last is not None and last.is_working():
            try:
                self._client.cancel_order(symbol=symbol, order_link_id=order_link_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("order_cancel_after_timeout_failed", order_link_id=order_link_id, error=str(exc))
        raise OrderNotFilledError(
            f"order {order_link_id} not filled within {cfg.fill_timeout_sec}s "
            f"(last_status={None if last is None else last.status})"
        )

    def ensure_protective_stops(
        self,
        symbol: str,
        *,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        instrument: Instrument | None = None,
    ) -> None:
        if stop_loss is None:
            raise StopLossMissingError(f"{symbol} position has no stop-loss to confirm")
        cfg = self._config.orders
        tick = None if instrument is None else instrument.tick_size
        for attempt in range(cfg.sl_confirm_attempts):
            if self._position_has_sl(symbol, stop_loss, tick=tick):
                logger.info("stop_loss_confirmed", symbol=symbol, stop_loss=str(stop_loss), attempt=attempt + 1)
                return
            try:
                self._client.set_trading_stop(
                    symbol=symbol,
                    stop_loss=decimal_str(stop_loss),
                    take_profit=None if take_profit is None else decimal_str(take_profit),
                    position_idx=cfg.position_idx,
                    tpsl_mode=cfg.tpsl_mode,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("set_trading_stop_failed", symbol=symbol, error=str(exc), attempt=attempt + 1)
            self._sleep(cfg.sl_confirm_delay_sec)
        if self._position_has_sl(symbol, stop_loss, tick=tick):
            logger.info("stop_loss_confirmed", symbol=symbol, stop_loss=str(stop_loss), attempt="final")
            return
        logger.critical(
            "stop_loss_missing",
            symbol=symbol,
            expected_sl=str(stop_loss),
            detail="protective stop was not confirmed on the exchange",
        )
        if self._db is not None:
            self._db.record_event(
                "stop_loss_missing",
                f"{symbol} SL {stop_loss} not confirmed on exchange",
                level="CRITICAL",
            )
        if cfg.flatten_on_missing_sl:
            self.flatten_symbol(symbol)
        raise StopLossMissingError(
            f"{symbol} stop-loss was not confirmed on the exchange after {cfg.sl_confirm_attempts} attempts"
        )

    def flatten_symbol(self, symbol: str, *, instrument: Instrument | None = None) -> ExchangeOrder | None:
        """Reduce-only market close. Allowed while the kill switch is active."""
        positions = _parse_positions(self._client.get_positions(symbol))
        pos = next((item for item in positions if item.symbol == symbol), None)
        if pos is None or pos.size <= 0:
            logger.info("flatten_skip_no_position", symbol=symbol)
            return None
        close_side = Side.SELL.value if pos.side in {"Buy", "BUY", "Long"} else Side.BUY.value
        qty = pos.size
        if instrument is not None:
            qty = instrument.validate_qty(qty, market=True)
        intent = OrderIntent(
            symbol=symbol,
            side=close_side,
            order_type="Market",
            quantity=qty,
            client_order_id=self.make_client_id("flt"),
            reduce_only=True,
            time_in_force=self._config.orders.time_in_force_market,
            position_idx=self._config.orders.position_idx,
        )
        logger.warning("flatten_market", symbol=symbol, side=close_side, qty=str(qty))
        return self.submit(intent, instrument=instrument)

    def cancel(self, symbol: str, order_link_id: str) -> None:
        self._client.cancel_order(symbol=symbol, order_link_id=order_link_id)
        fetched = self._fetch(symbol, order_link_id)
        if fetched:
            self._remember(fetched)

    def restore_from_exchange(self, symbol: str | None = None) -> list[ExchangeOrder]:
        """Exchange is source of truth on restart. Merge open orders into local state."""
        payload = self._client.get_open_orders(symbol)
        rows = (payload.get("result") or {}).get("list") or []
        out: list[ExchangeOrder] = []
        for row in rows:
            order = parse_exchange_order(row)
            self._remember(order)
            out.append(order)
        logger.info("orders_restored", count=len(out), symbol=symbol)
        return out

    def _assert_may_submit(self, intent: OrderIntent) -> None:
        if intent.reduce_only:
            if not self._config.mutating_orders_allowed():
                raise LiveOrdersBlockedError(
                    f"reduce-only orders blocked in mode={self._config.system.mode.value}"
                )
            return
        if not self._config.live_orders_allowed():
            raise LiveOrdersBlockedError(
                f"new orders blocked in mode={self._config.system.mode.value} "
                f"(live_trading_confirm={self._config.system.live_trading_confirm})"
            )
        if self._kill is not None:
            self._kill.assert_allows_new_orders()

    def _tif(self, order_type: str) -> str:
        if order_type.lower() == "market":
            return self._config.orders.time_in_force_market
        return self._config.orders.time_in_force_limit

    def _existing(self, order_link_id: str) -> ExchangeOrder | None:
        cached = self._cached(order_link_id)
        if cached is not None:
            return cached
        if self._db is None:
            return None
        record = self._db.get_order_by_client_id(order_link_id)
        if record is None:
            return None
        return ExchangeOrder(
            order_id=record.order_id,
            client_order_id=record.client_order_id,
            symbol=record.symbol,
            side=record.side,
            order_type=record.type,
            qty=record.quantity,
            price=record.price,
            status=record.status,
            filled_qty=to_decimal_payload(record.payload, "filled_qty", ZERO),
            avg_price=None,
            leaves_qty=None,
            reduce_only=bool((record.payload or {}).get("reduce_only")),
            reject_reason="",
            stop_loss=str((record.payload or {}).get("stop_loss") or ""),
            take_profit=str((record.payload or {}).get("take_profit") or ""),
            time_in_force="",
            raw=record.payload or {},
        )

    def _db_terminal_filled(self, order_link_id: str) -> bool:
        if self._db is None:
            return False
        record = self._db.get_order_by_client_id(order_link_id)
        return record is not None and record.status in FILLED_STATUSES

    def _cached(self, order_link_id: str) -> ExchangeOrder | None:
        with self._lock:
            return self._by_link.get(order_link_id)

    def _fetch(self, symbol: str, order_link_id: str) -> ExchangeOrder | None:
        row = self._client.get_order(symbol=symbol, order_link_id=order_link_id)
        if row is None:
            return None
        return parse_exchange_order(row, fallback_symbol=symbol)

    def _remember(self, order: ExchangeOrder) -> None:
        if not order.client_order_id:
            return
        with self._lock:
            self._by_link[order.client_order_id] = order
        self._persist(order)

    def _persist(self, order: ExchangeOrder) -> None:
        if self._db is None:
            return
        now = utc_now()
        self._db.upsert_order(
            OrderRecord(
                order_id=order.order_id or order.client_order_id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                type=order.order_type,
                price=order.avg_price or order.price,
                quantity=order.qty,
                status=order.status or "Created",
                created_at=now,
                updated_at=now,
                session_id=self.session_id,
                payload={
                    "filled_qty": str(order.filled_qty),
                    "avg_price": None if order.avg_price is None else str(order.avg_price),
                    "reduce_only": order.reduce_only,
                    "reject_reason": order.reject_reason,
                    "stop_loss": order.stop_loss,
                    "take_profit": order.take_profit,
                    "exchange_order_id": order.order_id,
                },
            )
        )

    def _position_has_sl(self, symbol: str, expected: Decimal, *, tick: Decimal | None) -> bool:
        positions = _parse_positions(self._client.get_positions(symbol))
        for pos in positions:
            if pos.symbol != symbol:
                continue
            if sl_matches(pos.stop_loss, expected, tick=tick):
                return True
        return False


def to_decimal_payload(payload: dict[str, Any] | None, key: str, default: Decimal) -> Decimal:
    if not payload or payload.get(key) in (None, ""):
        return default
    return Decimal(str(payload[key]))
