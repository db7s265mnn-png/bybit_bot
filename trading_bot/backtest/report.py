from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_bot.backtest.engine import DISCLAIMER, BacktestResult
from trading_bot.backtest.metrics import compute_metrics


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def result_to_dict(result: BacktestResult, *, include_equity: bool = False) -> dict[str, Any]:
    payload = result.as_report()
    payload["metrics"] = _jsonable(result.metrics)
    if include_equity:
        payload["equity_curve"] = [
            {"time": ts.isoformat(), "equity": str(eq)} for ts, eq in result.equity_curve
        ]
    return _jsonable(payload)


def combined_trade_metrics(results: list[BacktestResult], interval_ms: int) -> dict[str, Any]:
    if not results:
        return {}
    trades = [t for r in results for t in r.trades]
    initial = results[0].initial_balance
    net = sum((t.net_pnl or Decimal("0")) for t in trades)
    fees = sum((t.fees or Decimal("0")) for t in trades)
    funding = sum((t.funding or Decimal("0")) for t in trades)
    slip = sum((t.slippage or Decimal("0")) for t in trades)
    equity = [initial]
    running = initial
    for trade in trades:
        running += trade.net_pnl or Decimal("0")
        equity.append(running)
    metrics = compute_metrics(
        initial_balance=initial,
        final_balance=running,
        equity=equity,
        trades=trades,
        interval_ms=interval_ms,
        total_fees=fees,
        total_funding=funding,
        total_slippage=slip,
    )
    return {
        "disclaimer": DISCLAIMER,
        "windows": len(results),
        "trades": len(trades),
        "metrics": _jsonable(metrics),
        "note": "Walk-forward combined stats concatenate test-window trades; they are not one continuous live account.",
    }
