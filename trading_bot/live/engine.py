from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trading_bot.account.service import (
    AccountService,
    AccountSnapshot,
    PositionSnapshot,
    has_protective_sl,
    parse_ws_position_message,
)
from trading_bot.backtest.engine import DISCLAIMER
from trading_bot.config.models import AppConfig, KillSwitchPolicy, TradingMode
from trading_bot.core.exceptions import StopLossMissingError, TradingBotError
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import ConnectionState, SignalType
from trading_bot.database.database import Database, PaperAccount
from trading_bot.exchange.instruments import Instrument, to_decimal
from trading_bot.execution.order_manager import OrderManager
from trading_bot.execution.order_state import OrderIntent
from trading_bot.execution.simulated import pending_from_json, pending_payloads
from trading_bot.market.candles import Candle, parse_ws_kline_message
from trading_bot.monitoring.logger import get_logger
from trading_bot.risk.position_sizing import loss_per_unit
from trading_bot.risk.risk_manager import OpenRisk, RiskManager
from trading_bot.strategy.base import Signal, Strategy

logger = get_logger("trading_bot.live")

TESTNET_DISCLAIMER = (
    DISCLAIMER
    + " Testnet orders are real on Bybit Testnet; fills here are not a claim of mainnet profitability."
)

ZERO = Decimal("0")


def require_testnet_live(config: AppConfig) -> None:
    """Phase 8 is testnet only. Paper/backtest have their own commands; mainnet is Phase 10."""
    mode = config.system.mode
    if mode is TradingMode.MAINNET:
        raise TradingBotError(
            "MODE=mainnet is not enabled in Phase 8. Set MODE=testnet. Mainnet live is Phase 10."
        )
    if mode is not TradingMode.TESTNET:
        raise TradingBotError(
            f"live command requires MODE=testnet (got {mode.value}). "
            "Use `python -m trading_bot paper` or `backtest` for simulated modes."
        )
    if not config.exchange.testnet:
        raise TradingBotError("MODE=testnet requires exchange.testnet=true")


def _is_long_side(side: str) -> bool:
    return side.lower() in {"buy", "long"}


def _signal_side(signal: Signal) -> str:
    if signal.type is SignalType.LONG:
        return "Buy"
    if signal.type is SignalType.SHORT:
        return "Sell"
    return ""


class LiveEngine:
    """Testnet loop: strategy + risk + OrderManager. Exchange is source of truth on restore.

    Strategy never places orders. New entries wait for the next confirmed kline after the signal.
    REST warmup/gap candles update history only — they do not submit.
    """

    def __init__(
        self,
        config: AppConfig,
        strategy: Strategy,
        risk: RiskManager,
        instruments: dict[str, Instrument] | Instrument,
        *,
        account: AccountService,
        orders: OrderManager,
        database: Database,
        kill_switch: KillSwitch,
        session_id: str = "testnet",
        dry_run: bool = False,
    ) -> None:
        require_testnet_live(config)
        self._config = config
        self._strategy = strategy
        self._risk = risk
        self._account = account
        self._orders = orders
        self._db = database
        self._kill = kill_switch
        self.session_id = session_id
        self.dry_run = dry_run
        self._lock = threading.RLock()
        self._paused = True
        self._history: dict[str, list[Candle]] = {}
        self._last_ms: dict[str, int] = {}
        self._pending: dict[str, Signal] = {}
        self._positions: dict[str, PositionSnapshot] = {}
        self._equity = ZERO
        self._initial_equity = ZERO
        self._orders_placed = 0
        self._flattens = 0
        if isinstance(instruments, Instrument):
            self._instruments = {instruments.symbol: instruments}
        else:
            self._instruments = dict(instruments)

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def last_ms(self) -> dict[str, int]:
        return dict(self._last_ms)

    @property
    def positions(self) -> dict[str, PositionSnapshot]:
        return dict(self._positions)

    @property
    def pending(self) -> dict[str, Signal]:
        return dict(self._pending)

    @property
    def equity(self) -> Decimal:
        return self._equity

    def set_paused(self, paused: bool, *, reason: str = "") -> None:
        with self._lock:
            if self._paused == paused:
                return
            self._paused = paused
            logger.warning(
                "live_trading_pause" if paused else "live_trading_resume",
                trading_paused=paused,
                reason=reason or ("public websocket not connected" if paused else "public websocket connected"),
            )
            self._db.record_event(
                "trading_paused" if paused else "trading_resumed",
                reason or ("paused" if paused else "resumed"),
            )

    def on_connection_state(self, state: ConnectionState | str, reason: str) -> None:
        value = state.value if isinstance(state, ConnectionState) else str(state)
        self.set_paused(value != ConnectionState.CONNECTED.value, reason=reason)

    def on_private_state(self, state: ConnectionState | str, reason: str) -> None:
        value = state.value if isinstance(state, ConnectionState) else str(state)
        logger.warning("live_private_ws_state", state=value, reason=reason)
        # Private WS is advisory. OrderManager already confirms fills over REST.

    def restore(self, snapshot: AccountSnapshot | None = None) -> bool:
        """Replace local positions from the exchange. SQLite only overlays cursor/risk/pending."""
        with self._lock:
            snap = snapshot if snapshot is not None else self._account.snapshot()
            self._equity = snap.wallet.equity_for(self._config.exchange.settle_coin)
            if self._initial_equity <= 0:
                self._initial_equity = self._equity
            account_row = self._db.load_paper_account(self.session_id)
            if account_row is not None:
                self._last_ms = dict(account_row.last_candles)
                self._risk.restore_state(
                    day_key=account_row.day_key,
                    day_start_equity=account_row.day_start_equity,
                    daily_realized=account_row.daily_realized,
                    consecutive_losses=account_row.consecutive_losses,
                )
                self._pending = pending_from_json(account_row.pending_json)
            open_sqlite = self._db.list_trades(session_id=self.session_id, open_only=True)
            sqlite_positions_ignored = len(open_sqlite)
            self._orders.restore_from_exchange()
            self._positions = {pos.symbol: pos for pos in snap.positions}
            for pos in list(self._positions.values()):
                if has_protective_sl(pos):
                    continue
                logger.critical(
                    "live_restore_missing_sl",
                    symbol=pos.symbol,
                    size=str(pos.size),
                    side=pos.side,
                    detail="exchange position has no stop-loss; flattening",
                )
                self._db.record_event(
                    "stop_loss_missing",
                    f"{pos.symbol} restored without SL; flatten",
                    level="CRITICAL",
                )
                if self._config.orders.flatten_on_missing_sl:
                    self._flatten(pos.symbol)
            if self.dry_run:
                self._positions = {
                    symbol: pos for symbol, pos in self._positions.items() if has_protective_sl(pos)
                }
            self._persist_account()
            logger.info(
                "live_restored",
                session_id=self.session_id,
                equity=str(self._equity),
                open_positions=len(self._positions),
                pending=len(self._pending),
                sqlite_open_trades_ignored=sqlite_positions_ignored,
                dry_run=self.dry_run,
                source="exchange",
            )
            return True

    def load_history(self, candles: list[Candle]) -> None:
        """Seed strategy history without placing orders."""
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
                self._queue_from_history(symbol)
            self._persist_account()

    def ingest_history(self, candles: list[Candle]) -> None:
        """Advance cursor from REST catch-up. Never submits."""
        with self._lock:
            for candle in candles:
                if not candle.confirmed:
                    continue
                self._append_history(candle)
                last = self._last_ms.get(candle.symbol, 0)
                if candle.start_ms > last:
                    self._last_ms[candle.symbol] = candle.start_ms
            for symbol in {c.symbol for c in candles if c.confirmed}:
                self._queue_from_history(symbol, replace_hold=True)
            self._persist_account()

    def ingest_confirmed(self, candles: list[Candle]) -> None:
        with self._lock:
            for candle in candles:
                if candle.confirmed:
                    self._step(candle)

    def handle_ws_message(self, payload: dict[str, Any]) -> None:
        topic = str(payload.get("topic") or "")
        if topic.startswith("kline."):
            self.ingest_confirmed(parse_ws_kline_message(payload))

    def handle_private_message(self, payload: dict[str, Any]) -> None:
        topic = str(payload.get("topic") or "")
        if topic == "order" or topic.startswith("order."):
            self._orders.apply_ws_message(payload)
            return
        if topic == "position" or topic.startswith("position."):
            rows = payload.get("data") or []
            if isinstance(rows, dict):
                rows = [rows]
            with self._lock:
                for item in rows:
                    symbol = str(item.get("symbol") or "")
                    if not symbol:
                        continue
                    size = to_decimal(item.get("size") or "0")
                    if size <= 0:
                        self._positions.pop(symbol, None)
                for pos in parse_ws_position_message(payload):
                    self._positions[pos.symbol] = pos

    def last_mark_candle(self) -> Candle | None:
        with self._lock:
            latest: Candle | None = None
            for hist in self._history.values():
                if hist and (latest is None or hist[-1].start_ms >= latest.start_ms):
                    latest = hist[-1]
            return latest

    def snapshot(self, *, mark: Candle | None = None) -> dict[str, Any]:
        with self._lock:
            kill = self._kill.snapshot()
            return {
                "disclaimer": TESTNET_DISCLAIMER,
                "session_id": self.session_id,
                "mode": "testnet",
                "real_orders": not self.dry_run,
                "dry_run": self.dry_run,
                "equity": self._equity,
                "initial_balance": self._initial_equity,
                "open_positions": [
                    {
                        "symbol": pos.symbol,
                        "side": pos.side,
                        "quantity": pos.size,
                        "entry": pos.avg_price,
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                    }
                    for pos in self._positions.values()
                ],
                "pending_signals": {sym: sig.type.value for sym, sig in self._pending.items()},
                "orders_placed": self._orders_placed,
                "flattens": self._flattens,
                "trading_paused": self._paused,
                "kill_switch_active": kill.active,
                "kill_switch_policy": kill.policy.value,
                "last_candles_ms": dict(self._last_ms),
                "strategy": self._strategy.name,
                "source_of_truth": "exchange",
            }

    def _step(self, candle: Candle) -> None:
        last = self._last_ms.get(candle.symbol, 0)
        if candle.start_ms <= last:
            return
        self._refresh_account()
        self._risk.note_session_equity(self._equity, candle.start_time)
        kill = self._kill.snapshot()
        if kill.active and kill.policy is KillSwitchPolicy.FLATTEN and candle.symbol in self._positions:
            self._flatten(candle.symbol)
        self._execute_pending(candle)
        self._append_history(candle)
        if self._entries_allowed():
            self._queue_from_history(candle.symbol)
        self._last_ms[candle.symbol] = candle.start_ms
        self._db.upsert_candles([candle])
        self._persist_account()

    def _entries_allowed(self) -> bool:
        return (not self._paused) and (not self._kill.is_active())

    def _queue_from_history(self, symbol: str, *, replace_hold: bool = False) -> None:
        hist = self._history.get(symbol) or []
        if len(hist) < self._strategy.required_history():
            return
        signal = self._strategy.generate_signal(hist)
        if signal.type is SignalType.HOLD:
            if replace_hold:
                self._pending.pop(symbol, None)
            return
        self._pending[symbol] = signal
        logger.info(
            "live_signal",
            symbol=signal.symbol,
            type=signal.type.value,
            stop_loss=str(signal.stop_loss) if signal.stop_loss is not None else None,
            execute_on="next_confirmed_kline",
        )

    def _execute_pending(self, candle: Candle) -> None:
        signal = self._pending.pop(candle.symbol, None)
        if signal is None:
            return
        if signal.type is SignalType.HOLD:
            return
        if signal.type is SignalType.EXIT:
            if candle.symbol in self._positions:
                self._flatten(candle.symbol)
            return
        if not self._entries_allowed():
            self._pending[candle.symbol] = signal
            logger.info(
                "live_entry_deferred",
                symbol=candle.symbol,
                paused=self._paused,
                kill_switch=self._kill.is_active(),
            )
            return
        desired = _signal_side(signal)
        current = self._positions.get(candle.symbol)
        if current is not None:
            same = _is_long_side(current.side) == _is_long_side(desired)
            if same:
                logger.info("live_skip_same_side", symbol=candle.symbol, side=current.side)
                return
            logger.info("live_reverse_flatten", symbol=candle.symbol, from_side=current.side, to_side=desired)
            self._flatten(candle.symbol)
            if candle.symbol in self._positions:
                logger.error("live_reverse_blocked", symbol=candle.symbol, detail="flatten did not clear position")
                return
        self._submit_entry(signal, candle)

    def _submit_entry(self, signal: Signal, candle: Candle) -> None:
        instrument = self._instruments.get(signal.symbol)
        if instrument is None:
            logger.error("live_missing_instrument", symbol=signal.symbol)
            return
        if signal.stop_loss is None:
            logger.error("live_entry_missing_sl", symbol=signal.symbol)
            return
        entry_price = candle.close
        decision = self._risk.evaluate(
            equity=self._equity,
            signal=signal,
            entry_price=entry_price,
            stop_loss=signal.stop_loss,
            instrument=instrument,
            open_positions=self._open_risks(),
            at=candle.start_time,
            leverage=self._config.trading.leverage,
        )
        if not decision.allowed or decision.quantity <= 0:
            logger.warning("live_entry_denied", symbol=signal.symbol, reason=decision.reason)
            return
        intent = OrderIntent(
            symbol=signal.symbol,
            side=_signal_side(signal),
            order_type="Market",
            quantity=decision.quantity,
            client_order_id=self._orders.make_client_id("ent"),
            reduce_only=False,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            time_in_force=self._config.orders.time_in_force_market,
            position_idx=self._config.orders.position_idx,
        )
        if self.dry_run:
            logger.info(
                "live_dry_run_intent",
                symbol=intent.symbol,
                side=intent.side,
                qty=str(intent.quantity),
                stop_loss=str(intent.stop_loss),
            )
            self._orders_placed += 1
            return
        try:
            filled = self._orders.submit(intent, instrument=instrument)
        except StopLossMissingError:
            logger.critical("live_entry_sl_missing_after_fill", symbol=signal.symbol)
            self._refresh_account()
            return
        except TradingBotError as exc:
            logger.error("live_entry_failed", symbol=signal.symbol, error=str(exc), error_type=type(exc).__name__)
            return
        self._orders_placed += 1
        logger.info(
            "live_entry_filled",
            symbol=signal.symbol,
            order_link_id=filled.client_order_id,
            status=filled.status,
            filled_qty=str(filled.filled_qty),
            avg_price=None if filled.avg_price is None else str(filled.avg_price),
        )
        self._refresh_account()

    def _flatten(self, symbol: str) -> None:
        instrument = self._instruments.get(symbol)
        if self.dry_run:
            logger.warning("live_dry_run_flatten", symbol=symbol)
            self._positions.pop(symbol, None)
            self._flattens += 1
            return
        try:
            result = self._orders.flatten_symbol(symbol, instrument=instrument)
        except TradingBotError as exc:
            logger.error("live_flatten_failed", symbol=symbol, error=str(exc))
            self._refresh_account()
            return
        self._flattens += 1
        if result is not None:
            logger.warning(
                "live_flattened",
                symbol=symbol,
                order_link_id=result.client_order_id,
                status=result.status,
            )
        self._refresh_account()

    def _refresh_account(self) -> None:
        try:
            wallet = self._account.wallet()
            self._equity = wallet.equity_for(self._config.exchange.settle_coin)
            self._positions = {pos.symbol: pos for pos in self._account.positions()}
        except TradingBotError as exc:
            logger.error("live_account_refresh_failed", error=str(exc))

    def _open_risks(self) -> list[OpenRisk]:
        out: list[OpenRisk] = []
        taker = self._config.fees.taker
        slip = self._config.execution.slippage
        for pos in self._positions.values():
            if not has_protective_sl(pos):
                continue
            stop = to_decimal(pos.stop_loss)
            signal_type = SignalType.LONG if _is_long_side(pos.side) else SignalType.SHORT
            unit = loss_per_unit(signal_type, pos.avg_price, stop, taker=taker, slippage=slip)
            out.append(OpenRisk(pos.symbol, pos.size * unit))
        return out

    def _append_history(self, candle: Candle) -> None:
        hist = self._history.setdefault(candle.symbol, [])
        if hist and hist[-1].start_ms == candle.start_ms:
            hist[-1] = candle
        elif not hist or hist[-1].start_ms < candle.start_ms:
            hist.append(candle)
        self._strategy.on_candle(candle, hist)

    def _persist_account(self) -> None:
        now = datetime.now(timezone.utc)
        initial = self._initial_equity if self._initial_equity > 0 else self._equity
        self._db.save_paper_account(
            PaperAccount(
                session_id=self.session_id,
                equity=self._equity,
                initial_balance=initial,
                last_candles=dict(self._last_ms),
                consecutive_losses=self._risk.consecutive_losses,
                day_key=self._risk.day_key,
                day_start_equity=self._risk.day_start_equity,
                daily_realized=self._risk.daily_realized,
                pending_json=pending_payloads(self._pending),
                strategy=self._strategy.name,
                updated_at=now,
            )
        )
