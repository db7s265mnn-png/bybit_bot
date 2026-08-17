from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

from trading_bot.database.database import TradeRecord

ZERO = Decimal("0")


def _as_float(values: Sequence[Decimal]) -> list[float]:
    return [float(v) for v in values]


def max_drawdown(equity: Sequence[Decimal]) -> Decimal:
    if not equity:
        return ZERO
    peak = equity[0]
    max_dd = ZERO
    for value in equity:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _ratio(returns: list[float], *, negative_only: bool, periods_per_year: float) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    if negative_only:
        sample = [r for r in returns if r < 0]
        if len(sample) < 2:
            return None
        center = 0.0
    else:
        sample = returns
        center = mean
    var = sum((r - center) ** 2 for r in sample) / (len(sample) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(periods_per_year)


def consecutive_losses(pnls: Sequence[Decimal]) -> int:
    best = 0
    current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def compute_metrics(
    *,
    initial_balance: Decimal,
    final_balance: Decimal,
    equity: Sequence[Decimal],
    trades: Sequence[TradeRecord],
    interval_ms: int,
    total_fees: Decimal,
    total_funding: Decimal,
    total_slippage: Decimal,
) -> dict[str, Decimal | int | float | None]:
    nets = [t.net_pnl or ZERO for t in trades if t.net_pnl is not None]
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p < 0]
    longs = sum(1 for t in trades if t.side.lower() in {"buy", "long"})
    shorts = sum(1 for t in trades if t.side.lower() in {"sell", "short"})
    gross = sum((t.gross_pnl or ZERO) for t in trades)
    net = final_balance - initial_balance
    roi = (final_balance - initial_balance) / initial_balance if initial_balance else ZERO
    bars_per_year = (365 * 24 * 3_600_000) / interval_ms if interval_ms else 1
    bar_returns: list[float] = []
    for prev, cur in zip(equity, equity[1:]):
        if prev != 0:
            bar_returns.append(float((cur - prev) / prev))
    win_sum = sum(wins, ZERO)
    loss_abs = abs(sum(losses, ZERO))
    profit_factor: Decimal | None
    if loss_abs == 0:
        profit_factor = None if win_sum == 0 else None
    else:
        profit_factor = win_sum / loss_abs
    if loss_abs == 0 and win_sum > 0:
        profit_factor = None  # undefined / infinite — omit a fake huge number
    return {
        "initial_balance": initial_balance,
        "final_balance": final_balance,
        "net_profit": net,
        "gross_pnl": gross,
        "roi": roi,
        "max_drawdown": max_drawdown(equity),
        "sharpe_ratio": _ratio(bar_returns, negative_only=False, periods_per_year=bars_per_year),
        "sortino_ratio": _ratio(bar_returns, negative_only=True, periods_per_year=bars_per_year),
        "win_rate": (Decimal(len(wins)) / Decimal(len(nets))) if nets else ZERO,
        "profit_factor": profit_factor,
        "average_win": (sum(wins, ZERO) / len(wins)) if wins else ZERO,
        "average_loss": (sum(losses, ZERO) / len(losses)) if losses else ZERO,
        "expectancy": (sum(nets, ZERO) / len(nets)) if nets else ZERO,
        "number_of_trades": len(trades),
        "long_trades": longs,
        "short_trades": shorts,
        "maximum_consecutive_losses": consecutive_losses(nets),
        "total_fees": total_fees,
        "total_funding": total_funding,
        "total_slippage": total_slippage,
        "net_pnl": sum(nets, ZERO) if nets else net,
    }
