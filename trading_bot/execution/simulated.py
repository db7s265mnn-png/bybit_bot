from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trading_bot.backtest.splits import funding_events_in_bar
from trading_bot.config.models import AppConfig
from trading_bot.core.ids import new_event_id
from trading_bot.core.types import Side, SignalType
from trading_bot.database.database import Database, OrderRecord, TradeRecord
from trading_bot.exchange.instruments import Instrument
from trading_bot.execution.slippage import fill_price, taker_fee
from trading_bot.market.candles import Candle, interval_to_ms
from trading_bot.risk.position_sizing import loss_per_unit
from trading_bot.risk.risk_manager import OpenRisk, RiskManager
from trading_bot.strategy.base import Signal

ZERO = Decimal("0")


@dataclass
class SimulatedPosition:
    trade_id: str
    symbol: str
    side: Side
    quantity: Decimal
    entry_ref: Decimal
    entry_fill: Decimal
    entry_time: datetime
    stop_loss: Decimal
    take_profit: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    risk_amount: Decimal
    opened_on_ms: int


@dataclass
class WorkingOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: str
    quantity: Decimal
    price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    status: str
    created_at: datetime
    fee_is_maker: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


def signal_to_payload(signal: Signal) -> dict[str, Any]:
    return {
        "type": signal.type.value,
        "symbol": signal.symbol,
        "timestamp": signal.timestamp.isoformat(),
        "confidence": str(signal.confidence),
        "stop_loss": None if signal.stop_loss is None else str(signal.stop_loss),
        "take_profit": None if signal.take_profit is None else str(signal.take_profit),
        "recommended_size": None if signal.recommended_size is None else str(signal.recommended_size),
        "extra": signal.extra,
    }


def signal_from_payload(payload: dict[str, Any]) -> Signal:
    ts = payload.get("timestamp")
    if isinstance(ts, datetime):
        timestamp = ts
    elif ts:
        timestamp = datetime.fromisoformat(str(ts))
    else:
        timestamp = datetime.now(timezone.utc)
    sl = payload.get("stop_loss")
    tp = payload.get("take_profit")
    rec = payload.get("recommended_size")
    return Signal(
        type=SignalType(str(payload["type"])),
        symbol=str(payload["symbol"]),
        timestamp=timestamp,
        confidence=Decimal(str(payload.get("confidence") or "0")),
        stop_loss=None if sl in (None, "") else Decimal(str(sl)),
        take_profit=None if tp in (None, "") else Decimal(str(tp)),
        recommended_size=None if rec in (None, "") else Decimal(str(rec)),
        extra=dict(payload.get("extra") or {}),
    )


class SimulatedBroker:
    """Virtual fills, fees, slippage, SL/TP, and funding. Never talks to the exchange."""

    def __init__(
        self,
        config: AppConfig,
        instruments: dict[str, Instrument] | Instrument,
        *,
        strategy_name: str,
        database: Database | None = None,
        session_id: str = "paper",
        initial_equity: Decimal | None = None,
    ) -> None:
        self._config = config
        if isinstance(instruments, Instrument):
            self._instruments = {instruments.symbol: instruments}
        else:
            self._instruments = dict(instruments)
        self._strategy_name = strategy_name
        self._db = database
        self.session_id = session_id
        self.initial_equity = initial_equity if initial_equity is not None else config.backtest.initial_balance
        self.equity = self.initial_equity
        self.positions: dict[str, SimulatedPosition] = {}
        self.pending: dict[str, Signal] = {}
        self.trades: list[TradeRecord] = []
        self.working: dict[str, WorkingOrder] = {}
        self.total_fees = ZERO
        self.total_funding = ZERO
        self.total_slippage = ZERO
        self.gross_pnl = ZERO
        self._pending_client_ids: dict[str, str] = {}

    def instrument(self, symbol: str) -> Instrument:
        if symbol in self._instruments:
            return self._instruments[symbol]
        if len(self._instruments) == 1:
            return next(iter(self._instruments.values()))
        raise KeyError(f"no instrument spec for {symbol}")

    def open_risks(self) -> list[OpenRisk]:
        return [OpenRisk(p.symbol, p.risk_amount) for p in self.positions.values()]

    def unrealized(self, candle: Candle) -> Decimal:
        pos = self.positions.get(candle.symbol)
        if pos is None:
            return ZERO
        sign = Decimal("1") if pos.side is Side.BUY else Decimal("-1")
        return (candle.close - pos.entry_fill) * pos.quantity * sign

    def queue_signal(self, signal: Signal, *, now: datetime | None = None) -> None:
        if signal.type is SignalType.HOLD:
            return
        self._cancel_pending_order(signal.symbol, now or signal.timestamp, reason="replaced")
        self.pending[signal.symbol] = signal
        created = now or signal.timestamp
        client_id = self._client_order_id()
        order_id = new_event_id()
        existing = self.positions.get(signal.symbol)
        if signal.type is SignalType.EXIT:
            side = Side.SELL if (existing is None or existing.side is Side.BUY) else Side.BUY
        elif signal.type is SignalType.LONG:
            side = Side.BUY
        else:
            side = Side.SELL
        working = WorkingOrder(
            order_id=order_id,
            client_order_id=client_id,
            symbol=signal.symbol,
            side=side,
            order_type="Market",
            quantity=ZERO,
            price=None,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            status="New",
            created_at=created,
            payload=signal_to_payload(signal),
        )
        if signal.type is SignalType.EXIT:
            working.payload["reduce_only"] = True
        self.working[client_id] = working
        self._pending_client_ids[signal.symbol] = client_id
        self._persist_working(working, updated_at=created)

    def place_limit(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        now: datetime,
        risk: RiskManager,
        allow: bool = True,
    ) -> WorkingOrder:
        """Simulated rest-on-book limit. Sized/checked at submit; filled when price trades through."""
        instrument = self.instrument(symbol)
        qty = instrument.validate_qty(quantity, market=False)
        rounded = instrument.round_price(price, side=side.value)
        signal_type = SignalType.LONG if side is Side.BUY else SignalType.SHORT
        dummy = Signal(
            type=signal_type,
            symbol=symbol,
            timestamp=now,
            confidence=Decimal("1"),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if not allow:
            raise PermissionError("new orders are paused")
        decision = risk.evaluate(
            equity=self.equity,
            signal=dummy,
            entry_price=rounded,
            stop_loss=stop_loss,
            instrument=instrument,
            open_positions=self.open_risks(),
            at=now,
        )
        if not decision.allowed:
            raise PermissionError(decision.reason)
        qty = min(qty, decision.quantity) if decision.quantity > 0 else qty
        qty = instrument.validate_qty(qty, market=False)
        client_id = self._client_order_id()
        order = WorkingOrder(
            order_id=new_event_id(),
            client_order_id=client_id,
            symbol=symbol,
            side=side,
            order_type="Limit",
            quantity=qty,
            price=rounded,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status="New",
            created_at=now,
            payload={"stop_loss": str(stop_loss), "take_profit": None if take_profit is None else str(take_profit)},
        )
        self.working[client_id] = order
        self._persist_working(order, updated_at=now)
        return order

    def process_market_candle(
        self,
        candle: Candle,
        risk: RiskManager,
        *,
        allow_entries: bool = True,
        flatten: bool = False,
    ) -> list[TradeRecord]:
        closed: list[TradeRecord] = []
        if flatten:
            closed.extend(self.flatten_symbol(candle, risk, reason="kill_flatten"))
            self._drop_pending(candle.start_time, reason="kill_flatten")
            for order in list(self.working.values()):
                if order.symbol == candle.symbol and order.status == "New":
                    self._set_working_status(order, "Cancelled", candle.start_time)
        else:
            signal = self.pending.pop(candle.symbol, None)
            if signal is not None:
                is_exit = signal.type is SignalType.EXIT
                if is_exit or (signal.is_entry() and allow_entries):
                    closed.extend(self._handle_signal(signal, candle, risk))
                else:
                    self._cancel_pending_order(signal.symbol, candle.start_time, reason="paused_or_blocked")
            self._fill_limits(candle, risk, allow_entries=allow_entries)

        pos = self.positions.get(candle.symbol)
        if pos is not None and pos.opened_on_ms < candle.start_ms:
            self._apply_funding(pos, candle)

        if candle.symbol in self.positions:
            hit = self._manage_intrabar(candle, risk)
            if hit is not None:
                closed.append(hit)
        return closed

    def flatten_symbol(self, candle: Candle, risk: RiskManager, *, reason: str = "flatten") -> list[TradeRecord]:
        pos = self.positions.get(candle.symbol)
        if pos is None:
            return []
        record = self._close_position(pos, candle, candle.open, reason, risk)
        self.positions.pop(candle.symbol, None)
        return [record]

    def close_all(self, candle: Candle, reason: str, risk: RiskManager) -> list[TradeRecord]:
        closed: list[TradeRecord] = []
        for pos in list(self.positions.values()):
            closed.append(self._close_position(pos, candle, candle.close, reason, risk))
        self.positions.clear()
        return closed

    def restore_position(self, trade: TradeRecord) -> SimulatedPosition:
        side = Side.BUY if trade.side in {"Buy", "BUY", "LONG"} else Side.SELL
        stop = trade.stop_loss if trade.stop_loss is not None else trade.entry_price
        take = trade.take_profit if trade.take_profit is not None else trade.entry_price
        signal_type = SignalType.LONG if side is Side.BUY else SignalType.SHORT
        risk_amount = trade.quantity * loss_per_unit(
            signal_type,
            trade.entry_price,
            stop,
            taker=self._config.fees.taker,
            slippage=self._config.execution.slippage,
        )
        opened_ms = int(trade.entry_time.timestamp() * 1000)
        pos = SimulatedPosition(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            side=side,
            quantity=trade.quantity,
            entry_ref=trade.entry_price,
            entry_fill=trade.entry_price,
            entry_time=trade.entry_time,
            stop_loss=stop,
            take_profit=take,
            fees=trade.fees or ZERO,
            funding=trade.funding or ZERO,
            slippage=trade.slippage or ZERO,
            risk_amount=risk_amount,
            opened_on_ms=opened_ms,
        )
        self.positions[trade.symbol] = pos
        return pos

    def restore_pending(self, signal: Signal) -> None:
        self.pending[signal.symbol] = signal

    def restore_working(self, order: OrderRecord) -> None:
        if order.status not in {"New", "Created", "Untriggered"}:
            return
        payload = order.payload or {}
        side = Side.BUY if order.side in {"Buy", "BUY", "LONG"} else Side.SELL
        sl = payload.get("stop_loss")
        tp = payload.get("take_profit")
        working = WorkingOrder(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=side,
            order_type=order.type,
            quantity=order.quantity,
            price=order.price,
            stop_loss=None if sl in (None, "") else Decimal(str(sl)),
            take_profit=None if tp in (None, "") else Decimal(str(tp)),
            status=order.status,
            created_at=order.created_at,
            payload=dict(payload),
        )
        if working.stop_loss is None and payload.get("type") in {t.value for t in SignalType}:
            try:
                signal = signal_from_payload(payload)
                working.stop_loss = signal.stop_loss
                working.take_profit = signal.take_profit
                self.pending[signal.symbol] = signal
                self._pending_client_ids[signal.symbol] = order.client_order_id
            except (KeyError, ValueError):
                pass
        self.working[order.client_order_id] = working

    def _handle_signal(self, signal: Signal, candle: Candle, risk: RiskManager) -> list[TradeRecord]:
        closed: list[TradeRecord] = []
        existing = self.positions.get(signal.symbol)
        if signal.type is SignalType.EXIT:
            if existing:
                record = self._close_position(existing, candle, candle.open, "signal_exit", risk)
                closed.append(record)
                self.positions.pop(signal.symbol, None)
                self._complete_pending_order(signal.symbol, candle.start_time, status="Filled", price=candle.open)
            else:
                self._cancel_pending_order(signal.symbol, candle.start_time, reason="no_position")
            return closed
        if not signal.is_entry() or signal.stop_loss is None:
            self._cancel_pending_order(signal.symbol, candle.start_time, reason="invalid_signal")
            return closed

        want_side = Side.BUY if signal.type is SignalType.LONG else Side.SELL
        if existing and existing.side is want_side:
            self._cancel_pending_order(signal.symbol, candle.start_time, reason="already_in_position")
            return closed
        if existing and existing.side is not want_side:
            record = self._close_position(existing, candle, candle.open, "reverse", risk)
            closed.append(record)
            self.positions.pop(signal.symbol, None)

        opened = self._open_market(signal, candle, risk)
        if opened is None:
            self._cancel_pending_order(signal.symbol, candle.start_time, reason="risk_rejected")
        else:
            self._complete_pending_order(
                signal.symbol, candle.start_time, status="Filled", price=opened.entry_fill
            )
        return closed

    def _open_market(self, signal: Signal, candle: Candle, risk: RiskManager) -> SimulatedPosition | None:
        instrument = self.instrument(signal.symbol)
        want_side = Side.BUY if signal.type is SignalType.LONG else Side.SELL
        entry_ref = candle.open
        entry_fill = instrument.round_price(
            fill_price(
                entry_ref,
                signal.type,
                slippage=self._config.execution.slippage,
                spread=self._config.execution.spread,
                is_entry=True,
            ),
            side="Buy" if want_side is Side.BUY else "Sell",
        )
        stop = signal.stop_loss
        if stop is None:
            return None
        take = signal.take_profit or self._recompute_tp(signal.type, entry_fill, stop)
        decision = risk.evaluate(
            equity=self.equity,
            signal=signal,
            entry_price=entry_fill,
            stop_loss=stop,
            instrument=instrument,
            open_positions=self.open_risks(),
            at=candle.start_time,
        )
        if not decision.allowed or decision.quantity <= 0:
            return None
        qty = decision.quantity
        entry_fee = taker_fee(qty * entry_fill, self._config.fees.taker)
        slip_cost = abs(entry_fill - entry_ref) * qty
        trade_id = new_event_id()
        pos = SimulatedPosition(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=want_side,
            quantity=qty,
            entry_ref=entry_ref,
            entry_fill=entry_fill,
            entry_time=candle.start_time,
            stop_loss=stop,
            take_profit=take,
            fees=entry_fee,
            funding=ZERO,
            slippage=slip_cost,
            risk_amount=decision.risk_amount,
            opened_on_ms=candle.start_ms,
        )
        self.positions[signal.symbol] = pos
        self._persist_open_trade(pos)
        return pos

    def _fill_limits(self, candle: Candle, risk: RiskManager, *, allow_entries: bool) -> None:
        for client_id, order in list(self.working.items()):
            if order.symbol != candle.symbol or order.order_type != "Limit" or order.status != "New":
                continue
            if not allow_entries:
                continue
            fill_ref, maker = self._limit_fill_ref(order, candle)
            if fill_ref is None:
                continue
            signal_type = SignalType.LONG if order.side is Side.BUY else SignalType.SHORT
            stop = order.stop_loss
            if stop is None:
                self._set_working_status(order, "Rejected", candle.start_time)
                continue
            dummy = Signal(
                type=signal_type,
                symbol=order.symbol,
                timestamp=candle.start_time,
                confidence=Decimal("1"),
                stop_loss=stop,
                take_profit=order.take_profit,
            )
            if order.symbol in self.positions:
                existing = self.positions[order.symbol]
                if existing.side is order.side:
                    self._set_working_status(order, "Cancelled", candle.start_time)
                    continue
                self._close_position(existing, candle, fill_ref, "reverse", risk)
                self.positions.pop(order.symbol, None)
            instrument = self.instrument(order.symbol)
            if maker:
                entry_fill = instrument.round_price(fill_ref, side=order.side.value)
                slip_cost = ZERO
                fee = taker_fee(order.quantity * entry_fill, self._config.fees.maker)
            else:
                entry_fill = instrument.round_price(
                    fill_price(
                        fill_ref,
                        signal_type,
                        slippage=self._config.execution.slippage,
                        spread=self._config.execution.spread,
                        is_entry=True,
                    ),
                    side=order.side.value,
                )
                slip_cost = abs(entry_fill - fill_ref) * order.quantity
                fee = taker_fee(order.quantity * entry_fill, self._config.fees.taker)
            decision = risk.evaluate(
                equity=self.equity,
                signal=dummy,
                entry_price=entry_fill,
                stop_loss=stop,
                instrument=instrument,
                open_positions=self.open_risks(),
                at=candle.start_time,
            )
            if not decision.allowed:
                self._set_working_status(order, "Cancelled", candle.start_time)
                continue
            qty = min(order.quantity, decision.quantity)
            take = order.take_profit or self._recompute_tp(signal_type, entry_fill, stop)
            trade_id = new_event_id()
            pos = SimulatedPosition(
                trade_id=trade_id,
                symbol=order.symbol,
                side=order.side,
                quantity=qty,
                entry_ref=fill_ref,
                entry_fill=entry_fill,
                entry_time=candle.start_time,
                stop_loss=stop,
                take_profit=take,
                fees=fee,
                funding=ZERO,
                slippage=slip_cost,
                risk_amount=decision.risk_amount,
                opened_on_ms=candle.start_ms,
            )
            self.positions[order.symbol] = pos
            order.status = "Filled"
            order.quantity = qty
            order.price = entry_fill
            self._persist_working(order, updated_at=candle.start_time)
            self._persist_open_trade(pos)
            self.working.pop(client_id, None)

    @staticmethod
    def _limit_fill_ref(order: WorkingOrder, candle: Candle) -> tuple[Decimal, bool] | None:
        if order.price is None:
            return None
        price = order.price
        if order.side is Side.BUY:
            if candle.open <= price:
                return candle.open, False
            if candle.low <= price:
                return price, True
            return None
        if candle.open >= price:
            return candle.open, False
        if candle.high >= price:
            return price, True
        return None

    def _manage_intrabar(self, candle: Candle, risk: RiskManager) -> TradeRecord | None:
        pos = self.positions.get(candle.symbol)
        if pos is None:
            return None
        hit = self._intrabar_exit(pos, candle)
        if hit is None:
            return None
        reason, fill_ref = hit
        closed = self._close_position(pos, candle, fill_ref, reason, risk)
        self.positions.pop(candle.symbol, None)
        return closed

    def _intrabar_exit(self, pos: SimulatedPosition, candle: Candle) -> tuple[str, Decimal] | None:
        long = pos.side is Side.BUY
        if long and candle.open <= pos.stop_loss:
            return "stop_loss", candle.open
        if not long and candle.open >= pos.stop_loss:
            return "stop_loss", candle.open
        if long and candle.open >= pos.take_profit:
            return "take_profit", candle.open
        if not long and candle.open <= pos.take_profit:
            return "take_profit", candle.open
        sl_hit = candle.low <= pos.stop_loss if long else candle.high >= pos.stop_loss
        tp_hit = candle.high >= pos.take_profit if long else candle.low <= pos.take_profit
        if sl_hit and tp_hit:
            return "stop_loss", pos.stop_loss
        if sl_hit:
            return "stop_loss", pos.stop_loss
        if tp_hit:
            return "take_profit", pos.take_profit
        return None

    def _apply_funding(self, pos: SimulatedPosition, candle: Candle) -> None:
        interval_ms = interval_to_ms(candle.interval)
        events = funding_events_in_bar(
            candle.start_ms, interval_ms, self._config.fees.funding_interval_hours
        )
        if events and self._config.fees.assumed_funding_rate:
            rate = self._config.fees.assumed_funding_rate * Decimal(events)
            payment = pos.quantity * candle.open * rate
            if pos.side is Side.SELL:
                payment = -payment
            pos.funding += payment

    def _close_position(
        self,
        pos: SimulatedPosition,
        candle: Candle,
        exit_ref: Decimal,
        reason: str,
        risk: RiskManager,
    ) -> TradeRecord:
        instrument = self.instrument(pos.symbol)
        exit_signal = SignalType.LONG if pos.side is Side.BUY else SignalType.SHORT
        exit_fill = instrument.round_price(
            fill_price(
                exit_ref,
                exit_signal,
                slippage=self._config.execution.slippage,
                spread=self._config.execution.spread,
                is_entry=False,
            ),
            side="Sell" if pos.side is Side.BUY else "Buy",
        )
        qty = pos.quantity
        sign = Decimal("1") if pos.side is Side.BUY else Decimal("-1")
        gross = (exit_ref - pos.entry_ref) * qty * sign
        slip = pos.slippage + abs(exit_fill - exit_ref) * qty
        exit_fee = taker_fee(qty * exit_fill, self._config.fees.taker)
        fees = pos.fees + exit_fee
        funding = pos.funding
        net = gross - fees - funding - slip
        record = TradeRecord(
            trade_id=pos.trade_id,
            symbol=pos.symbol,
            side=pos.side.value,
            entry_price=pos.entry_fill,
            exit_price=exit_fill,
            quantity=qty,
            entry_time=pos.entry_time,
            exit_time=candle.start_time,
            gross_pnl=gross,
            fees=fees,
            funding=funding,
            slippage=slip,
            net_pnl=net,
            strategy=self._strategy_name,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            session_id=self.session_id,
        )
        self.trades.append(record)
        self.equity += net
        self.gross_pnl += gross
        self.total_fees += fees
        self.total_funding += funding
        self.total_slippage += slip
        risk.record_closed_trade(net)
        if self._db is not None:
            self._db.upsert_trade(record)
            self._db.record_event(
                reason,
                f"closed {pos.symbol} {pos.side.value} net={net}",
                timestamp=candle.start_time,
            )
            self._persist_fill_order(pos, candle.start_time, order_type="Market", reduce_only=True, price=exit_fill)
        return record

    def _recompute_tp(self, signal_type: SignalType, entry: Decimal, stop: Decimal) -> Decimal:
        risk = abs(entry - stop)
        rr = self._config.strategy.params.take_profit.risk_reward
        if signal_type is SignalType.LONG:
            return entry + risk * rr
        return entry - risk * rr

    def _persist_open_trade(self, pos: SimulatedPosition) -> None:
        if self._db is None:
            return
        self._db.upsert_trade(
            TradeRecord(
                trade_id=pos.trade_id,
                symbol=pos.symbol,
                side=pos.side.value,
                entry_price=pos.entry_fill,
                exit_price=None,
                quantity=pos.quantity,
                entry_time=pos.entry_time,
                exit_time=None,
                gross_pnl=None,
                fees=pos.fees,
                funding=pos.funding,
                slippage=pos.slippage,
                net_pnl=None,
                strategy=self._strategy_name,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                session_id=self.session_id,
            )
        )

    def _persist_fill_order(
        self,
        pos: SimulatedPosition,
        now: datetime,
        *,
        order_type: str,
        reduce_only: bool = False,
        price: Decimal | None = None,
    ) -> None:
        if self._db is None:
            return
        oid = new_event_id() if reduce_only else pos.trade_id
        self._db.upsert_order(
            OrderRecord(
                order_id=oid,
                client_order_id=f"{self.session_id[:8]}-{oid[:16]}",
                symbol=pos.symbol,
                side=pos.side.value if not reduce_only else ("Sell" if pos.side is Side.BUY else "Buy"),
                type=order_type,
                price=price if price is not None else pos.entry_fill,
                quantity=pos.quantity,
                status="Filled",
                created_at=now,
                updated_at=now,
                session_id=self.session_id,
                payload={"reduce_only": reduce_only},
            )
        )

    def _persist_working(self, order: WorkingOrder, *, updated_at: datetime) -> None:
        if self._db is None:
            return
        self._db.upsert_order(
            OrderRecord(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side.value,
                type=order.order_type,
                price=order.price,
                quantity=order.quantity,
                status=order.status,
                created_at=order.created_at,
                updated_at=updated_at,
                session_id=self.session_id,
                payload=order.payload,
            )
        )

    def _set_working_status(self, order: WorkingOrder, status: str, now: datetime) -> None:
        order.status = status
        self._persist_working(order, updated_at=now)
        if status != "New":
            self.working.pop(order.client_order_id, None)

    def _cancel_pending_order(self, symbol: str, now: datetime, *, reason: str) -> None:
        client_id = self._pending_client_ids.pop(symbol, None)
        self.pending.pop(symbol, None)
        if not client_id or client_id not in self.working:
            return
        self._set_working_status(self.working[client_id], "Cancelled", now)
        if self._db is not None:
            self._db.record_event("order_cancelled", f"{symbol} {reason}", timestamp=now)

    def _complete_pending_order(
        self, symbol: str, now: datetime, *, status: str, price: Decimal | None
    ) -> None:
        client_id = self._pending_client_ids.pop(symbol, None)
        if not client_id or client_id not in self.working:
            return
        order = self.working[client_id]
        order.status = status
        if price is not None:
            order.price = price
        self._persist_working(order, updated_at=now)
        self.working.pop(client_id, None)

    def _drop_pending(self, now: datetime, *, reason: str) -> None:
        for symbol in list(self.pending):
            self._cancel_pending_order(symbol, now, reason=reason)

    def _client_order_id(self) -> str:
        raw = f"p{self.session_id[:4]}{new_event_id()[:20]}"
        return raw[:36]


def pending_payloads(pending: dict[str, Signal]) -> str:
    return json.dumps({symbol: signal_to_payload(signal) for symbol, signal in pending.items()}, default=str)


def pending_from_json(raw: str | None) -> dict[str, Signal]:
    if not raw:
        return {}
    data = json.loads(raw)
    return {symbol: signal_from_payload(payload) for symbol, payload in data.items()}
