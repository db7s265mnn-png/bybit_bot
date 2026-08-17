from __future__ import annotations

from typing import Any

__all__ = ["DISCLAIMER", "BacktestEngine", "BacktestResult", "run_backtest_suite"]


def __getattr__(name: str) -> Any:
    if name in {"DISCLAIMER", "BacktestEngine", "BacktestResult"}:
        from trading_bot.backtest.engine import BacktestEngine, BacktestResult, DISCLAIMER

        return {
            "DISCLAIMER": DISCLAIMER,
            "BacktestEngine": BacktestEngine,
            "BacktestResult": BacktestResult,
        }[name]
    if name == "run_backtest_suite":
        from trading_bot.backtest.runner import run_backtest_suite

        return run_backtest_suite
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
