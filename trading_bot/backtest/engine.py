from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_bot.backtest.metrics import compute_metrics
from trading_bot.backtest.splits import bar_close_time, fallback_instrument
from trading_bot.config.models import AppConfig
from trading_bot.core.types import SignalType
from trading_bot.database.database import Database, TradeRecord
from trading_bot.exchange.instruments import Instrument
from trading_bot.execution.simulated import SimulatedBroker
from trading_bot.market.candles import Candle, interval_to_ms
from trading_bot.risk.risk_manager import RiskManager
from trading_bot.strategy.base import Strategy

ZERO = Decimal("0")
DISCLAIMER = (
    "WARNING: Historical performance does not guarantee future results. "
    "This baseline is for infrastructure testing only and is not a claim that the strategy is profitable."
)


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
        broker = SimulatedBroker(
            self._config,
            self._instrument,
            strategy_name=self._strategy.name,
            database=self._db,
            session_id="backtest",
        )
        notes: list[str] = [DISCLAIMER]
        curve: list[tuple[datetime, Decimal]] = []

        for index, candle in enumerate(candles):
            if not candle.confirmed:
                notes.append(f"skipped unconfirmed candle {candle.start_time.isoformat()}")
                continue
            self._risk.note_session_equity(broker.equity, candle.start_time)
            if index >= execute_from:
                broker.process_market_candle(candle, self._risk)
            marked = broker.equity + broker.unrealized(candle)
            curve.append((bar_close_time(candle.start_time, interval_ms), marked))
            history = candles[: index + 1]
            self._strategy.on_candle(candle, history)
            new_signal = self._strategy.generate_signal(history)
            if new_signal.type is not SignalType.HOLD:
                broker.queue_signal(new_signal)

        broker.close_all(candles[-1], "eod", self._risk)

        if self._db is not None:
            for trade in broker.trades:
                self._db.upsert_trade(trade)

        metrics = compute_metrics(
            initial_balance=broker.initial_equity,
            final_balance=broker.equity,
            equity=[point[1] for point in curve],
            trades=broker.trades,
            interval_ms=interval_ms,
            total_fees=broker.total_fees,
            total_funding=broker.total_funding,
            total_slippage=broker.total_slippage,
        )
        return BacktestResult(
            initial_balance=broker.initial_equity,
            final_balance=broker.equity,
            trades=list(broker.trades),
            equity_curve=curve,
            total_fees=broker.total_fees,
            total_funding=broker.total_funding,
            total_slippage=broker.total_slippage,
            gross_pnl=broker.gross_pnl,
            net_pnl=broker.equity - broker.initial_equity,
            metrics=metrics,
            notes=notes,
            label=label,
        )


def resolve_instrument(config: AppConfig, symbol: str, database: Database | None) -> tuple[Instrument, str]:
    category = config.exchange.category.value
    if database is not None:
        cached = database.load_instrument(category, symbol)
        if cached is not None:
            return cached, "database"
    instrument = fallback_instrument(symbol, config.backtest.fallback_instrument, category=category)
    return instrument, "config_fallback"
