from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_bot.config.models import AppConfig
from trading_bot.core.ids import new_event_id
from trading_bot.core.types import Side, SignalType
from trading_bot.database.database import Database, OrderRecord, TradeRecord
from trading_bot.exchange.instruments import Instrument
from trading_bot.execution.slippage import fill_price, taker_fee
from trading_bot.market.candles import Candle, interval_to_ms
from trading_bot.risk.risk_manager import OpenRisk, RiskManager
from trading_bot.strategy.base import Signal, Strategy
from trading_bot.backtest.metrics import compute_metrics
from trading_bot.backtest.splits import bar_close_time, fallback_instrument, funding_events_in_bar

ZERO = Decimal("0")
DISCLAIMER = (
    "WARNING: Historical performance does not guarantee future results. "
    "This baseline is for infrastructure testing only and is not a claim that the strategy is profitable."
)


@dataclass
class _LivePosition:
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
class BacktestResult:
    initial_balance: Decimal
    final_balance: Decimal
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    total_fees: Decimal = ZERO
    total_funding: Decimal = ZERO
    total_slippage: Decimal = ZERO
    gross_pnl: Decimal = ZERO
    net_pnl: Decimal = ZERO
    metrics: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    label: str = "full"

    def as_report(self) -> dict:
        payload = {
            "label": self.label,
            "disclaimer": DISCLAIMER,
            "initial_balance": self.initial_balance,
            "final_balance": self.final_balance,
            "gross_pnl": self.gross_pnl,
            "trading_fees": self.total_fees,
            "funding": self.total_funding,
            "slippage": self.total_slippage,
            "net_pnl": self.net_pnl,
            "metrics": self.metrics,
            "trades": len(self.trades),
            "equity_curve_points": len(self.equity_curve),
            "notes": self.notes,
        }
        return payload


class BacktestEngine:
    """Event-driven backtest: signal on close of N, fill at next bar open. No look-ahead."""

    def __init__(
        self,
        config: AppConfig,
        strategy: Strategy,
        risk: RiskManager,
        instrument: Instrument,
        *,
        database: Database | None = None,
    ) -> None:
        self._config = config
        self._strategy = strategy
        self._risk = risk
        self._instrument = instrument
        self._db = database

    def run(
        self,
        candles: list[Candle],
        *,
        execute_from: int = 0,
        label: str = "full",
    ) -> BacktestResult:
        candles = [c for c in candles if c.confirmed]
        if not candles:
            raise ValueError("no confirmed candles")
        interval_ms = interval_to_ms(candles[0].interval)
        equity = self._config.backtest.initial_balance
        initial = equity
        positions: dict[str, _LivePosition] = {}
        pending: dict[str, Signal] = {}
        trades: list[TradeRecord] = []
        curve: list[tuple[datetime, Decimal]] = []
        notes: list[str] = [DISCLAIMER]
        total_fees = ZERO
        total_funding = ZERO
        total_slippage = ZERO
        gross_pnl = ZERO

        for index, candle in enumerate(candles):
            if not candle.confirmed:
                notes.append(f"skipped unconfirmed candle {candle.start_time.isoformat()}")
                continue
            self._risk.note_session_equity(equity, candle.start_time)

            if index >= execute_from:
                signal = pending.pop(candle.symbol, None)
                if signal is not None:
                    equity, closed = self._handle_signal(signal, candle, positions, trades, equity)
                    for item in closed:
                        gross_pnl += item.gross_pnl or ZERO
                        total_fees += item.fees or ZERO
                        total_funding += item.funding or ZERO
                        total_slippage += item.slippage or ZERO

                pos = positions.get(candle.symbol)
                if pos is not None and pos.opened_on_ms < candle.start_ms:
                    events = funding_events_in_bar(
                        candle.start_ms, interval_ms, self._config.fees.funding_interval_hours
                    )
                    if events and self._config.fees.assumed_funding_rate:
                        rate = self._config.fees.assumed_funding_rate * Decimal(events)
                        payment = pos.quantity * candle.open * rate
                        if pos.side is Side.SELL:
                            payment = -payment
                        pos.funding += payment

                if candle.symbol in positions:
                    closed_now = self._manage_intrabar(candle, positions, trades)
                    if closed_now:
                        equity += closed_now.net_pnl or ZERO
                        gross_pnl += closed_now.gross_pnl or ZERO
                        total_fees += closed_now.fees or ZERO
                        total_funding += closed_now.funding or ZERO
                        total_slippage += closed_now.slippage or ZERO
                        self._risk.record_closed_trade(closed_now.net_pnl or ZERO)

            marked = equity + self._unrealized(positions, candle)
            curve.append((bar_close_time(candle.start_time, interval_ms), marked))

            history = candles[: index + 1]
            self._strategy.on_candle(candle, history)
            new_signal = self._strategy.generate_signal(history)
            if new_signal.type is not SignalType.HOLD:
                pending[candle.symbol] = new_signal

        for pos in list(positions.values()):
            last = candles[-1]
            closed = self._close(
                pos,
                last,
                last.close,
                "eod",
                trades,
            )
            equity += closed.net_pnl or ZERO
            gross_pnl += closed.gross_pnl or ZERO
            total_fees += closed.fees or ZERO
            total_funding += closed.funding or ZERO
            total_slippage += closed.slippage or ZERO
            self._risk.record_closed_trade(closed.net_pnl or ZERO)
        positions.clear()

        if self._db is not None:
            for trade in trades:
                self._db.upsert_trade(trade)

        metrics = compute_metrics(
            initial_balance=initial,
            final_balance=equity,
            equity=[point[1] for point in curve],
            trades=trades,
            interval_ms=interval_ms,
            total_fees=total_fees,
            total_funding=total_funding,
            total_slippage=total_slippage,
        )
        return BacktestResult(
            initial_balance=initial,
            final_balance=equity,
            trades=trades,
            equity_curve=curve,
            total_fees=total_fees,
            total_funding=total_funding,
            total_slippage=total_slippage,
            gross_pnl=gross_pnl,
            net_pnl=equity - initial,
            metrics=metrics,
            notes=notes,
            label=label,
        )

    def _handle_signal(
        self,
        signal: Signal,
        candle: Candle,
        positions: dict[str, _LivePosition],
        trades: list[TradeRecord],
        equity: Decimal,
    ) -> tuple[Decimal, list[TradeRecord]]:
        closed: list[TradeRecord] = []
        existing = positions.get(signal.symbol)
        if signal.type is SignalType.EXIT:
            if existing:
                record = self._close(existing, candle, candle.open, "signal_exit", trades)
                closed.append(record)
                equity += record.net_pnl or ZERO
                self._risk.record_closed_trade(record.net_pnl or ZERO)
                positions.pop(signal.symbol, None)
            return equity, closed
        if not signal.is_entry() or signal.stop_loss is None:
            return equity, closed

        want_side = Side.BUY if signal.type is SignalType.LONG else Side.SELL
        if existing and existing.side is want_side:
            return equity, closed
        if existing and existing.side is not want_side:
            record = self._close(existing, candle, candle.open, "reverse", trades)
            closed.append(record)
            equity += record.net_pnl or ZERO
            self._risk.record_closed_trade(record.net_pnl or ZERO)
            positions.pop(signal.symbol, None)

        entry_ref = candle.open
        entry_fill = self._instrument.round_price(
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
        take = signal.take_profit or self._recompute_tp(signal.type, entry_fill, stop)
        open_risks = [OpenRisk(p.symbol, p.risk_amount) for p in positions.values()]
        decision = self._risk.evaluate(
            equity=equity,
            signal=signal,
            entry_price=entry_fill,
            stop_loss=stop,
            instrument=self._instrument,
            open_positions=open_risks,
            at=candle.start_time,
        )
        if not decision.allowed or decision.quantity <= 0:
            return equity, closed

        qty = decision.quantity
        entry_fee = taker_fee(qty * entry_fill, self._config.fees.taker)
        slip_cost = abs(entry_fill - entry_ref) * qty
        trade_id = new_event_id()
        positions[signal.symbol] = _LivePosition(
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
        if self._db is not None:
            now = candle.start_time
            self._db.upsert_order(
                OrderRecord(
                    order_id=trade_id,
                    client_order_id=f"bt-{trade_id[:16]}",
                    symbol=signal.symbol,
                    side=want_side.value,
                    type="Market",
                    price=entry_fill,
                    quantity=qty,
                    status="Filled",
                    created_at=now,
                    updated_at=now,
                )
            )
        return equity, closed

    def _manage_intrabar(
        self,
        candle: Candle,
        positions: dict[str, _LivePosition],
        trades: list[TradeRecord],
    ) -> TradeRecord | None:
        pos = positions.get(candle.symbol)
        if pos is None:
            return None
        hit = self._intrabar_exit(pos, candle)
        if hit is None:
            return None
        reason, fill_ref = hit
        closed = self._close(pos, candle, fill_ref, reason, trades)
        positions.pop(candle.symbol, None)
        return closed

    def _intrabar_exit(self, pos: _LivePosition, candle: Candle) -> tuple[str, Decimal] | None:
        long = pos.side is Side.BUY
        # Gap through the stop at the open: fill at open, not at a better price.
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

    def _close(
        self,
        pos: _LivePosition,
        candle: Candle,
        exit_ref: Decimal,
        reason: str,
        trades: list[TradeRecord],
    ) -> TradeRecord:
        exit_signal = SignalType.LONG if pos.side is Side.BUY else SignalType.SHORT
        exit_fill = self._instrument.round_price(
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
            strategy=self._strategy.name,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
        )
        trades.append(record)
        if self._db is not None:
            self._db.record_event(
                reason,
                f"closed {pos.symbol} {pos.side.value} net={net}",
                timestamp=candle.start_time,
            )
        return record

    def _recompute_tp(self, signal_type: SignalType, entry: Decimal, stop: Decimal) -> Decimal:
        risk = abs(entry - stop)
        rr = self._config.strategy.params.take_profit.risk_reward
        if signal_type is SignalType.LONG:
            return entry + risk * rr
        return entry - risk * rr

    @staticmethod
    def _unrealized(positions: dict[str, _LivePosition], candle: Candle) -> Decimal:
        pos = positions.get(candle.symbol)
        if pos is None:
            return ZERO
        sign = Decimal("1") if pos.side is Side.BUY else Decimal("-1")
        return (candle.close - pos.entry_fill) * pos.quantity * sign


def resolve_instrument(config: AppConfig, symbol: str, database: Database | None) -> tuple[Instrument, str]:
    category = config.exchange.category.value
    if database is not None:
        cached = database.load_instrument(category, symbol)
        if cached is not None:
            return cached, "database"
    instrument = fallback_instrument(symbol, config.backtest.fallback_instrument, category=category)
    return instrument, "config_fallback"
