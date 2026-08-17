from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trading_bot.backtest.engine import DISCLAIMER
from trading_bot.config.models import AppConfig, KillSwitchPolicy
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import ConnectionState, SignalType
from trading_bot.database.database import Database, PaperAccount
from trading_bot.exchange.instruments import Instrument
from trading_bot.execution.simulated import (
    SimulatedBroker,
    pending_from_json,
    pending_payloads,
)
from trading_bot.market.candles import Candle, parse_ws_kline_message
from trading_bot.monitoring.logger import get_logger
from trading_bot.risk.risk_manager import RiskManager
from trading_bot.strategy.base import Strategy

logger = get_logger("trading_bot.paper")

PAPER_DISCLAIMER = (
    DISCLAIMER
    + " Paper trading sends no real orders; simulated fills are not a claim of live profitability."
)

ZERO = Decimal("0")


class PaperEngine:
    """Realtime paper loop: strategy + risk + simulated broker. Never places exchange orders."""

    def __init__(
        self,
        config: AppConfig,
        strategy: Strategy,
        risk: RiskManager,
        instruments: dict[str, Instrument] | Instrument,
        *,
        database: Database,
        kill_switch: KillSwitch,
        session_id: str = "paper",
    ) -> None:
        self._config = config
        self._strategy = strategy
        self._risk = risk
        self._db = database
        self._kill = kill_switch
        self.session_id = session_id
        self._lock = threading.RLock()
        self._paused = True
        self._flattened = False
        self._history: dict[str, list[Candle]] = {}
        self._last_ms: dict[str, int] = {}
        if isinstance(instruments, Instrument):
            inst_map = {instruments.symbol: instruments}
        else:
            inst_map = dict(instruments)
        self._broker = SimulatedBroker(
            config,
            inst_map,
            strategy_name=strategy.name,
            database=database,
            session_id=session_id,
        )
        self._real_orders = False

    @property
    def broker(self) -> SimulatedBroker:
        return self._broker

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool, *, reason: str = "") -> None:
        with self._lock:
            if self._paused == paused:
                return
            self._paused = paused
            logger.warning(
                "paper_trading_pause" if paused else "paper_trading_resume",
                trading_paused=paused,
                reason=reason or ("websocket not connected" if paused else "websocket connected"),
            )
            self._db.record_event(
                "trading_paused" if paused else "trading_resumed",
                reason or ("paused" if paused else "resumed"),
            )

    def restore(self) -> bool:
        """Rebuild virtual account, open positions, and pending signals from SQLite."""
        with self._lock:
            account = self._db.load_paper_account(self.session_id)
            restored = False
            if account is not None:
                self._broker.equity = account.equity
                self._broker.initial_equity = account.initial_balance
                self._last_ms = dict(account.last_candles)
                self._risk.restore_state(
                    day_key=account.day_key,
                    day_start_equity=account.day_start_equity,
                    daily_realized=account.daily_realized,
                    consecutive_losses=account.consecutive_losses,
                )
                for signal in pending_from_json(account.pending_json).values():
                    self._broker.restore_pending(signal)
                restored = True
            for trade in self._db.list_trades(session_id=self.session_id, open_only=True):
                self._broker.restore_position(trade)
                restored = True
            for order in self._db.list_orders(session_id=self.session_id, status="New"):
                self._broker.restore_working(order)
                restored = True
            closed = [
                t
                for t in self._db.list_trades(session_id=self.session_id)
                if t.exit_price is not None
            ]
            self._broker.trades = list(closed)
            self._broker.gross_pnl = sum((t.gross_pnl or ZERO for t in closed), ZERO)
            self._broker.total_fees = sum((t.fees or ZERO for t in closed), ZERO)
            self._broker.total_funding = sum((t.funding or ZERO for t in closed), ZERO)
            self._broker.total_slippage = sum((t.slippage or ZERO for t in closed), ZERO)
            if restored:
                logger.info(
                    "paper_restored",
                    session_id=self.session_id,
                    equity=str(self._broker.equity),
                    open_positions=len(self._broker.positions),
                    pending=len(self._broker.pending),
                )
            return restored

    @property
    def last_ms(self) -> dict[str, int]:
        return dict(self._last_ms)

    def load_history(self, candles: list[Candle]) -> None:
        """Seed strategy history without opening virtual trades."""
        with self._lock:
            for candle in candles:
                if candle.confirmed:
                    self._append_history(candle)
            confirmed = [c for c in candles if c.confirmed]
            if confirmed:
                self._db.upsert_candles(confirmed)

    def seed_if_fresh(self) -> None:
        """On a new session, mark warmup as processed and queue the first live signal."""
        with self._lock:
            if self._last_ms:
                return
            for symbol, hist in self._history.items():
                if not hist:
                    continue
                self._last_ms[symbol] = hist[-1].start_ms
                signal = self._strategy.generate_signal(hist)
                if signal.type is not SignalType.HOLD:
                    self._broker.queue_signal(signal)
            self._persist_account()

    def warmup(self, candles: list[Candle]) -> None:
        self.load_history(candles)
        self.seed_if_fresh()

    def replay(self, candles: list[Candle]) -> None:
        """Run confirmed candles through the paper broker (CSV / DB, no exchange orders)."""
        with self._lock:
            self._paused = False
            for candle in candles:
                if not candle.confirmed:
                    continue
                if candle.start_ms <= self._last_ms.get(candle.symbol, 0):
                    self._append_history(candle)
                    continue
                self._step(candle)

    def ingest_confirmed(self, candles: list[Candle]) -> None:
        with self._lock:
            for candle in candles:
                if candle.confirmed:
                    self._step(candle)

    def handle_ws_message(self, payload: dict[str, Any]) -> None:
        topic = str(payload.get("topic") or "")
        if not topic.startswith("kline."):
            return
        self.ingest_confirmed(parse_ws_kline_message(payload))

    def on_connection_state(self, state: ConnectionState | str, reason: str) -> None:
        value = state.value if isinstance(state, ConnectionState) else str(state)
        self.set_paused(value != ConnectionState.CONNECTED.value, reason=reason)

    def last_mark_candle(self) -> Candle | None:
        with self._lock:
            latest: Candle | None = None
            for hist in self._history.values():
                if hist and (latest is None or hist[-1].start_ms >= latest.start_ms):
                    latest = hist[-1]
            return latest

    def snapshot(self, *, mark: Candle | None = None) -> dict[str, Any]:
        with self._lock:
            unreal = ZERO if mark is None else self._broker.unrealized(mark)
            kill = self._kill.snapshot()
            return {
                "disclaimer": PAPER_DISCLAIMER,
                "session_id": self.session_id,
                "mode": "paper",
                "real_orders": self._real_orders,
                "equity": self._broker.equity,
                "marked_equity": self._broker.equity + unreal,
                "initial_balance": self._broker.initial_equity,
                "gross_pnl": self._broker.gross_pnl,
                "trading_fees": self._broker.total_fees,
                "funding": self._broker.total_funding,
                "slippage": self._broker.total_slippage,
                "net_pnl": self._broker.equity - self._broker.initial_equity,
                "open_positions": [
                    {
                        "symbol": pos.symbol,
                        "side": pos.side.value,
                        "quantity": pos.quantity,
                        "entry": pos.entry_fill,
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                    }
                    for pos in self._broker.positions.values()
                ],
                "closed_trades": len(self._broker.trades),
                "pending_signals": {sym: sig.type.value for sym, sig in self._broker.pending.items()},
                "trading_paused": self._paused,
                "kill_switch_active": kill.active,
                "kill_switch_policy": kill.policy.value,
                "last_candles_ms": dict(self._last_ms),
                "strategy": self._strategy.name,
            }

    def _step(self, candle: Candle) -> None:
        last = self._last_ms.get(candle.symbol, 0)
        if candle.start_ms <= last:
            return
        self._risk.note_session_equity(self._broker.equity, candle.start_time)
        kill = self._kill.snapshot()
        flatten = False
        if kill.active and kill.policy is KillSwitchPolicy.FLATTEN and candle.symbol in self._broker.positions:
            flatten = True
            self._flattened = True
        allow_entries = (not self._paused) and (not kill.active)
        closed = self._broker.process_market_candle(
            candle,
            self._risk,
            allow_entries=allow_entries,
            flatten=flatten,
        )
        self._append_history(candle)
        if allow_entries:
            hist = self._history[candle.symbol]
            signal = self._strategy.generate_signal(hist)
            if signal.type is not SignalType.HOLD:
                self._broker.queue_signal(signal)
                logger.info(
                    "paper_signal",
                    symbol=signal.symbol,
                    type=signal.type.value,
                    stop_loss=str(signal.stop_loss) if signal.stop_loss is not None else None,
                )
        self._last_ms[candle.symbol] = candle.start_ms
        self._db.upsert_candles([candle])
        self._persist_account()
        for trade in closed:
            logger.info(
                "paper_trade_closed",
                symbol=trade.symbol,
                net_pnl=str(trade.net_pnl),
                trade_id=trade.trade_id,
            )

    def _append_history(self, candle: Candle) -> None:
        hist = self._history.setdefault(candle.symbol, [])
        if hist and hist[-1].start_ms == candle.start_ms:
            hist[-1] = candle
        elif not hist or hist[-1].start_ms < candle.start_ms:
            hist.append(candle)
        self._strategy.on_candle(candle, hist)

    def _persist_account(self) -> None:
        now = datetime.now(timezone.utc)
        self._db.save_paper_account(
            PaperAccount(
                session_id=self.session_id,
                equity=self._broker.equity,
                initial_balance=self._broker.initial_equity,
                last_candles=dict(self._last_ms),
                consecutive_losses=self._risk.consecutive_losses,
                day_key=self._risk.day_key,
                day_start_equity=self._risk.day_start_equity,
                daily_realized=self._risk.daily_realized,
                pending_json=pending_payloads(self._broker.pending),
                strategy=self._strategy.name,
                updated_at=now,
            )
        )
