from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class TradingMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    TESTNET = "testnet"
    MAINNET = "mainnet"


class ProductCategory(str, Enum):
    SPOT = "spot"
    LINEAR = "linear"
    INVERSE = "inverse"


class KillSwitchPolicy(str, Enum):
    HOLD = "hold"
    FLATTEN = "flatten"


class TakeProfitType(str, Enum):
    FIXED = "fixed"
    ATR = "atr"
    RISK_REWARD = "risk_reward"


class ExchangeConfig(BaseModel):
    testnet: bool = True
    category: ProductCategory = ProductCategory.LINEAR
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    rest_timeout_sec: int = Field(default=10, ge=1, le=60)
    # bybit.com is the global host. bytick.com is the documented alternate domain.
    domain: str = "bybit"
    max_requests_per_second: float = Field(default=8.0, gt=0, le=50)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_base_delay_sec: float = Field(default=0.5, gt=0)
    retry_max_delay_sec: float = Field(default=8.0, gt=0)

    @field_validator("symbols")
    @classmethod
    def symbols_upper(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip().upper() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("exchange.symbols must contain at least one symbol")
        return cleaned

    @field_validator("account_type", "settle_coin")
    @classmethod
    def upper_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("domain")
    @classmethod
    def domain_allowed(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in {"bybit", "bytick"}:
            raise ValueError("exchange.domain must be 'bybit' or 'bytick'")
        return cleaned


class TradingConfig(BaseModel):
    timeframe: str = "15m"
    leverage: Decimal = Decimal("1")

    @field_validator("timeframe")
    @classmethod
    def timeframe_supported(cls, value: str) -> str:
        allowed = {
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "12h",
            "1d",
            "1w",
        }
        if value not in allowed:
            raise ValueError(f"unsupported timeframe {value!r}; allowed: {sorted(allowed)}")
        return value


class RiskConfig(BaseModel):
    risk_per_trade: Decimal = Decimal("0.005")
    max_daily_loss: Decimal = Decimal("0.02")
    max_open_positions: int = Field(default=3, ge=1)
    max_position_size: Decimal = Decimal("0.25")
    max_portfolio_risk: Decimal = Decimal("0.015")
    max_consecutive_losses: int = Field(default=5, ge=1)
    max_leverage: Decimal = Decimal("5")


class ExecutionConfig(BaseModel):
    slippage: Decimal = Decimal("0.001")
    spread: Decimal = Decimal("0")


class FeesConfig(BaseModel):
    taker: Decimal = Decimal("0.00055")
    maker: Decimal = Decimal("0.0002")
    # Per funding interval, used in backtest when historical funding is not loaded.
    assumed_funding_rate: Decimal = Decimal("0")
    funding_interval_hours: int = Field(default=8, ge=1, le=24)


class TakeProfitConfig(BaseModel):
    type: TakeProfitType = TakeProfitType.RISK_REWARD
    risk_reward: Decimal = Decimal("2.0")
    atr_multiplier: Decimal = Decimal("3.0")
    fixed_pct: Decimal = Decimal("0.01")


class StrategyParams(BaseModel):
    model_config = {"extra": "allow"}

    fast_ema: int = 20
    slow_ema: int = 50
    atr_period: int = 14
    atr_sl_multiplier: Decimal = Decimal("2.0")
    take_profit: TakeProfitConfig = Field(default_factory=TakeProfitConfig)


class StrategyConfig(BaseModel):
    name: str = "ema_crossover"
    params: StrategyParams = Field(default_factory=StrategyParams)


class KillSwitchConfig(BaseModel):
    position_policy: KillSwitchPolicy = KillSwitchPolicy.HOLD


class WebSocketConfig(BaseModel):
    ping_interval_sec: float = Field(default=20.0, ge=5, le=25)
    ping_timeout_sec: float = Field(default=10.0, ge=3)
    stale_after_sec: float = Field(default=40.0, ge=10)
    max_reconnect_attempts: int = Field(default=20, ge=1, le=100)
    reconnect_base_delay_sec: float = Field(default=1.0, gt=0)
    reconnect_max_delay_sec: float = Field(default=60.0, gt=0)


class SystemConfig(BaseModel):
    mode: TradingMode = TradingMode.PAPER
    log_level: str = "INFO"
    live_trading_confirm: bool = False

    @field_validator("log_level")
    @classmethod
    def log_level_upper(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return level


class SplitConfig(BaseModel):
    development: Decimal = Decimal("0.6")
    validation: Decimal = Decimal("0.2")
    out_of_sample: Decimal = Decimal("0.2")

    @model_validator(mode="after")
    def fractions_sum_to_one(self) -> SplitConfig:
        total = self.development + self.validation + self.out_of_sample
        if abs(total - Decimal("1")) > Decimal("0.001"):
            raise ValueError("backtest.split fractions must sum to 1")
        return self


class WalkForwardConfig(BaseModel):
    enabled: bool = True
    train_fraction: Decimal = Decimal("0.4")
    test_fraction: Decimal = Decimal("0.2")
    step_fraction: Decimal = Decimal("0.2")


class BacktestFallbackInstrument(BaseModel):
    """Used only when instruments-info is not in the DB and the API is unreachable."""

    tick_size: Decimal = Decimal("0.1")
    qty_step: Decimal = Decimal("0.001")
    min_order_qty: Decimal = Decimal("0.001")
    max_order_qty: Decimal = Decimal("1000")
    min_notional: Decimal = Decimal("5")


class BacktestConfig(BaseModel):
    initial_balance: Decimal = Decimal("10000")
    split: SplitConfig = Field(default_factory=SplitConfig)
    walk_forward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    fallback_instrument: BacktestFallbackInstrument = Field(default_factory=BacktestFallbackInstrument)


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///data/trading_bot.db"


class TelegramConfig(BaseModel):
    enabled: bool = False
    allowed_user_ids: list[int] = Field(default_factory=list)
    heartbeat_interval_sec: int = Field(default=60, ge=10)
    stale_market_alert_sec: int = Field(default=30, ge=5)


class OrdersConfig(BaseModel):
    """Live/testnet order-manager tunables. Paper/backtest never hit these APIs."""

    fill_timeout_sec: float = Field(default=15.0, gt=0, le=120)
    poll_interval_sec: float = Field(default=0.25, gt=0, le=5)
    sl_confirm_attempts: int = Field(default=3, ge=1, le=10)
    sl_confirm_delay_sec: float = Field(default=0.4, gt=0, le=10)
    position_idx: int = Field(default=0, ge=0, le=2)
    time_in_force_market: str = "IOC"
    time_in_force_limit: str = "GTC"
    flatten_on_missing_sl: bool = True
    tpsl_mode: str = "Full"


class Secrets(BaseModel):
    """Loaded from environment / .env only. Never written to YAML."""

    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    def has_bybit_keys(self) -> bool:
        return bool(self.bybit_api_key and self.bybit_api_secret)

    def __repr__(self) -> str:
        return (
            "Secrets(bybit_api_key="
            f"{_mask(self.bybit_api_key)}, bybit_api_secret=***, "
            f"telegram_bot_token={_mask(self.telegram_bot_token)}, telegram_chat_id={_mask(self.telegram_chat_id)})"
        )


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


class AppConfig(BaseModel):
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    fees: FeesConfig = Field(default_factory=FeesConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    secrets: Secrets = Field(default_factory=Secrets)

    @model_validator(mode="after")
    def validate_mode_environment(self) -> AppConfig:
        mode = self.system.mode
        testnet = self.exchange.testnet
        if mode is TradingMode.TESTNET and not testnet:
            raise ValueError("system.mode=testnet requires exchange.testnet=true")
        if mode is TradingMode.MAINNET and testnet:
            raise ValueError("system.mode=mainnet requires exchange.testnet=false")
        if self.trading.leverage > self.risk.max_leverage:
            raise ValueError("trading.leverage cannot exceed risk.max_leverage")
        return self

    def requires_bybit_keys(self) -> bool:
        return self.system.mode in {TradingMode.TESTNET, TradingMode.MAINNET}

    def mutating_orders_allowed(self) -> bool:
        """Cancel / trading-stop / reduce-only flatten. Never in paper or backtest."""
        return self.system.mode in {TradingMode.TESTNET, TradingMode.MAINNET}

    def live_orders_allowed(self) -> bool:
        """New risk-increasing orders. Mainnet also requires LIVE_TRADING_CONFIRM."""
        if self.system.mode is TradingMode.MAINNET:
            return self.system.live_trading_confirm
        return self.system.mode is TradingMode.TESTNET

    def public_summary(self) -> dict[str, Any]:
        """Safe dict for logs: no secrets."""
        return {
            "mode": self.system.mode.value,
            "testnet": self.exchange.testnet,
            "category": self.exchange.category.value,
            "symbols": list(self.exchange.symbols),
            "timeframe": self.trading.timeframe,
            "live_orders_allowed": self.live_orders_allowed(),
            "mutating_orders_allowed": self.mutating_orders_allowed(),
            "has_api_keys": self.secrets.has_bybit_keys(),
        }
