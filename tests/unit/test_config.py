from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from trading_bot.config.loader import load_config
from trading_bot.config.models import TradingMode


@pytest.mark.unit
def test_yaml_keeps_risk_as_decimal(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("BYBIT_TESTNET", "true")
    config = load_config(config_path)
    assert config.risk.risk_per_trade == Decimal("0.005")
    assert config.risk.max_daily_loss == Decimal("0.02")
    assert config.execution.slippage == Decimal("0.001")


@pytest.mark.unit
def test_mode_testnet_requires_testnet_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "exchange": {"testnet": False, "symbols": ["BTCUSDT"]},
                "system": {"mode": "paper"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODE", "testnet")
    monkeypatch.setenv("BYBIT_TESTNET", "false")
    with pytest.raises(ValueError, match="testnet"):
        load_config(path)


@pytest.mark.unit
def test_mainnet_blocks_orders_without_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "exchange": {"testnet": False, "symbols": ["BTCUSDT"]},
                "system": {"mode": "mainnet", "live_trading_confirm": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("BYBIT_TESTNET", raising=False)
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    config = load_config(path)
    assert config.system.mode is TradingMode.MAINNET
    assert config.live_orders_allowed() is False
    assert config.mutating_orders_allowed() is True


@pytest.mark.unit
def test_testnet_allows_orders_without_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "exchange": {"testnet": True, "symbols": ["BTCUSDT"]},
                "system": {"mode": "testnet", "live_trading_confirm": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODE", "testnet")
    monkeypatch.setenv("BYBIT_TESTNET", "true")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "false")
    config = load_config(path)
    assert config.live_orders_allowed() is True
    assert config.mutating_orders_allowed() is True


@pytest.mark.unit
def test_env_overrides_mode(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "backtest")
    monkeypatch.setenv("BYBIT_TESTNET", "true")
    config = load_config(config_path)
    assert config.system.mode is TradingMode.BACKTEST


@pytest.mark.unit
def test_secrets_repr_hides_secret(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", "abcd1234")
    monkeypatch.setenv("BYBIT_API_SECRET", "super-secret-value")
    monkeypatch.setenv("MODE", "paper")
    config = load_config(config_path)
    dumped = repr(config.secrets)
    assert "super-secret-value" not in dumped
    assert "***" in dumped
