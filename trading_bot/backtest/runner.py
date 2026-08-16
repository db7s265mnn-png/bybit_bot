from __future__ import annotations

from trading_bot.backtest.engine import BacktestEngine, BacktestResult, resolve_instrument
from trading_bot.backtest.report import combined_trade_metrics, result_to_dict
from trading_bot.backtest.splits import split_ranges, walk_forward_windows
from trading_bot.config.models import AppConfig
from trading_bot.core.kill_switch import KillSwitch
from trading_bot.database.database import Database
from trading_bot.exchange.instruments import Instrument
from trading_bot.market.candles import Candle, interval_to_ms
from trading_bot.risk.risk_manager import RiskManager
from trading_bot.strategy import create_strategy


def _run_once(
    config: AppConfig,
    candles: list[Candle],
    instrument: Instrument,
    *,
    execute_from: int,
    label: str,
    database: Database | None,
) -> BacktestResult:
    strategy = create_strategy(config.strategy.name, params=config.strategy.params)
    risk = RiskManager(config, KillSwitch(config.kill_switch.position_policy))
    engine = BacktestEngine(config, strategy, risk, instrument, database=database)
    return engine.run(candles, execute_from=execute_from, label=label)


def run_backtest_suite(
    config: AppConfig,
    candles: list[Candle],
    *,
    instrument: Instrument | None = None,
    database: Database | None = None,
    persist_full: bool = False,
) -> dict:
    candles = [c for c in candles if c.confirmed]
    if not candles:
        raise ValueError("no confirmed candles")
    if instrument is None:
        instrument, source = resolve_instrument(config, candles[0].symbol, database)
    else:
        source = "provided"
    lookback = create_strategy(config.strategy.name, params=config.strategy.params).required_history()
    interval_ms = interval_to_ms(candles[0].interval)
    full = _run_once(
        config,
        candles,
        instrument,
        execute_from=0,
        label="full",
        database=database if persist_full else None,
    )
    payload: dict = {
        "disclaimer": full.notes[0] if full.notes else "",
        "instrument_source": source,
        "symbol": candles[0].symbol,
        "interval": candles[0].interval,
        "bars": len(candles),
        "strategy": config.strategy.name,
        "full": result_to_dict(full, include_equity=True),
        "splits": {},
        "walk_forward": {"enabled": config.backtest.walk_forward.enabled, "windows": []},
    }
    n = len(candles)
    split_cfg = config.backtest.split
    for rng in split_ranges(n, split_cfg.development, split_cfg.validation, split_cfg.out_of_sample):
        if rng.end - rng.start < lookback + 5:
            payload["splits"][rng.name] = {"skipped": True, "reason": "not enough bars"}
            continue
        warmup_start = max(0, rng.start - lookback)
        window = candles[warmup_start : rng.end]
        execute_from = rng.start - warmup_start
        result = _run_once(
            config, window, instrument, execute_from=execute_from, label=rng.name, database=None
        )
        payload["splits"][rng.name] = result_to_dict(result)

    wf = config.backtest.walk_forward
    if wf.enabled:
        windows = walk_forward_windows(
            n,
            train_fraction=wf.train_fraction,
            test_fraction=wf.test_fraction,
            step_fraction=wf.step_fraction,
        )
        wf_results: list[BacktestResult] = []
        for train_start, test_start, test_end in windows:
            window = candles[train_start:test_end]
            execute_from = test_start - train_start
            if execute_from < lookback:
                continue
            result = _run_once(
                config,
                window,
                instrument,
                execute_from=execute_from,
                label=f"wf_{test_start}_{test_end}",
                database=None,
            )
            wf_results.append(result)
            payload["walk_forward"]["windows"].append(result_to_dict(result))
        payload["walk_forward"]["combined"] = combined_trade_metrics(wf_results, interval_ms)
    return payload
