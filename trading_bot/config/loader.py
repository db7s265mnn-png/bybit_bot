from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from trading_bot.config.models import AppConfig, Secrets

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return _project_root() / "config" / "config.yaml"


def _load_env_files() -> None:
    root = _project_root()
    for candidate in (root / ".env", root / "config" / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _parse_bool(value: str | None, default: bool | None = None) -> bool | None:
    if value is None or value == "":
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"cannot parse boolean from {value!r}")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_overrides() -> dict[str, Any]:
    overlay: dict[str, Any] = {"exchange": {}, "system": {}}

    testnet = _parse_bool(os.getenv("BYBIT_TESTNET"))
    if testnet is not None:
        overlay["exchange"]["testnet"] = testnet

    domain = os.getenv("BYBIT_DOMAIN")
    if domain:
        overlay["exchange"]["domain"] = domain.strip()

    mode = os.getenv("MODE") or os.getenv("SYSTEM_MODE")
    if mode:
        overlay["system"]["mode"] = mode.strip().lower()

    confirm = _parse_bool(os.getenv("LIVE_TRADING_CONFIRM"))
    if confirm is not None:
        overlay["system"]["live_trading_confirm"] = confirm

    log_level = os.getenv("LOG_LEVEL")
    if log_level:
        overlay["system"]["log_level"] = log_level

    if not overlay["exchange"]:
        overlay.pop("exchange")
    if not overlay["system"]:
        overlay.pop("system")
    return overlay


class _DecimalSafeLoader(yaml.SafeLoader):
    """Keep money ratios exact. YAML floats like 0.005 would otherwise become binary floats."""


def _construct_decimal(loader: yaml.Loader, node: yaml.Node) -> Decimal:
    return Decimal(str(loader.construct_scalar(node)))


_DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.load(handle, Loader=_DecimalSafeLoader) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a mapping at the top level")
    return data


def load_secrets() -> Secrets:
    _load_env_files()
    return Secrets(
        bybit_api_key=os.getenv("BYBIT_API_KEY", "").strip(),
        bybit_api_secret=os.getenv("BYBIT_API_SECRET", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load YAML tunables, overlay env flags, attach secrets from the environment."""
    _load_env_files()
    config_path = Path(path) if path else Path(os.getenv("CONFIG_PATH", default_config_path()))
    raw = _deep_merge(_load_yaml(config_path), _env_overrides())
    raw["secrets"] = load_secrets().model_dump()
    return AppConfig.model_validate(raw)
