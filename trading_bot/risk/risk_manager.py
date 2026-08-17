from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.config.models import AppConfig
from trading_bot.core.exceptions import KillSwitchActiveError
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.core.types import SignalType
from trading_bot.exchange.instruments import Instrument
from trading_bot.risk.position_sizing import loss_per_unit
from trading_bot.strategy.base import Signal


@dataclass(frozen=True)
class OpenRisk:
    symbol: str
    risk_amount: Decimal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    quantity: Decimal
    notional: Decimal
    risk_amount: Decimal


class RiskManager:
    """Position size = risk_amount / (stop distance + fees + slippage). Never a flat % of equity."""

    def __init__(self, config: AppConfig, kill_switch: KillSwitch | None = None) -> None:
        self._config = config
        self._kill = kill_switch
        self._day_key = ""
        self._day_start_equity = Decimal("0")
        self._daily_realized = Decimal("0")
        self._consecutive_losses = 0

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def daily_realized(self) -> Decimal:
        return self._daily_realized

    @property
    def day_key(self) -> str:
        return self._day_key

    @property
    def day_start_equity(self) -> Decimal:
        return self._day_start_equity

    def restore_state(
        self,
        *,
        day_key: str = "",
        day_start_equity: Decimal = Decimal("0"),
        daily_realized: Decimal = Decimal("0"),
        consecutive_losses: int = 0,
    ) -> None:
        self._day_key = day_key
        self._day_start_equity = day_start_equity
        self._daily_realized = daily_realized
        self._consecutive_losses = consecutive_losses

    def note_session_equity(self, equity: Decimal, at: datetime) -> None:
        day = at.astimezone(timezone.utc).date().isoformat()
        if day != self._day_key:
            self._day_key = day
            self._day_start_equity = equity
            self._daily_realized = Decimal("0")

    def record_closed_trade(self, net_pnl: Decimal) -> None:
        self._daily_realized += net_pnl
        if net_pnl < 0:
            self._consecutive_losses += 1
        elif net_pnl > 0:
            self._consecutive_losses = 0

    def evaluate(
        self,
        *,
        equity: Decimal,
        signal: Signal,
        entry_price: Decimal,
        stop_loss: Decimal,
        instrument: Instrument,
        open_positions: list[OpenRisk],
        at: datetime,
        leverage: Decimal | None = None,
    ) -> RiskDecision:
        self.note_session_equity(equity, at)
        denied = self._hard_blocks(equity, signal, open_positions)
        if denied is not None:
            return denied
        if not signal.is_entry() or signal.stop_loss is None:
            return RiskDecision(False, "signal is not an entry with stop-loss", Decimal("0"), Decimal("0"), Decimal("0"))
        loss_per_unit = self._loss_per_unit(signal.type, entry_price, stop_loss)
        if loss_per_unit <= 0:
            return RiskDecision(False, "stop-loss is on the wrong side of entry or zero distance", Decimal("0"), Decimal("0"), Decimal("0"))
        risk_amount = equity * self._config.risk.risk_per_trade
        quantity = risk_amount / loss_per_unit
        lev = leverage if leverage is not None else self._config.trading.leverage
        max_notional_leverage = equity * lev
        max_notional_pct = equity * self._config.risk.max_position_size
        max_notional = min(max_notional_leverage, max_notional_pct)
        if entry_price > 0 and quantity * entry_price > max_notional:
            quantity = max_notional / entry_price
        try:
            quantity = instrument.validate_qty(quantity, market=True)
        except Exception as exc:  # noqa: BLE001 — convert instrument rejection to a deny
            return RiskDecision(False, str(exc), Decimal("0"), Decimal("0"), Decimal("0"))
        if instrument.min_notional is not None:
            try:
                instrument.validate_notional(quantity, entry_price)
            except Exception as exc:  # noqa: BLE001
                return RiskDecision(False, str(exc), Decimal("0"), Decimal("0"), Decimal("0"))
        trade_risk = quantity * loss_per_unit
        portfolio_risk = sum((item.risk_amount for item in open_positions), Decimal("0")) + trade_risk
        cap = equity * self._config.risk.max_portfolio_risk
        if portfolio_risk > cap:
            return RiskDecision(
                False,
                f"portfolio risk {portfolio_risk} exceeds cap {cap}",
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
            )
        if instrument.max_leverage is not None and lev > instrument.max_leverage:
            return RiskDecision(False, "leverage above instrument maxLeverage", Decimal("0"), Decimal("0"), Decimal("0"))
        if lev > self._config.risk.max_leverage:
            return RiskDecision(False, "leverage above risk.max_leverage", Decimal("0"), Decimal("0"), Decimal("0"))
        return RiskDecision(True, "ok", quantity, quantity * entry_price, trade_risk)

    def _hard_blocks(self, equity: Decimal, signal: Signal, open_positions: list[OpenRisk]) -> RiskDecision | None:
        if self._kill is not None:
            try:
                self._kill.assert_allows_new_orders()
            except KillSwitchActiveError as exc:
                return RiskDecision(False, str(exc), Decimal("0"), Decimal("0"), Decimal("0"))
        if self._day_start_equity > 0:
            limit = self._day_start_equity * self._config.risk.max_daily_loss
            if self._daily_realized <= -limit:
                return RiskDecision(
                    False,
                    "daily loss limit reached; trading halted for the day",
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                )
        if self._consecutive_losses >= self._config.risk.max_consecutive_losses:
            return RiskDecision(
                False,
                f"max consecutive losses ({self._config.risk.max_consecutive_losses}) reached",
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
            )
        if len(open_positions) >= self._config.risk.max_open_positions:
            if signal.symbol not in {item.symbol for item in open_positions}:
                return RiskDecision(
                    False,
                    f"max open positions ({self._config.risk.max_open_positions}) reached",
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                )
        if equity <= 0:
            return RiskDecision(False, "equity is not positive", Decimal("0"), Decimal("0"), Decimal("0"))
        return None

    def _loss_per_unit(self, signal_type: SignalType, entry: Decimal, stop: Decimal) -> Decimal:
        return loss_per_unit(
            signal_type,
            entry,
            stop,
            taker=self._config.fees.taker,
            slippage=self._config.execution.slippage,
        )
