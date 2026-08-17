from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_bot.exchange.bybit_client import BybitRESTClient
from trading_bot.exchange.instruments import optional_decimal, to_decimal


@dataclass(frozen=True)
class CoinBalance:
    coin: str
    equity: Decimal
    wallet_balance: Decimal
    usd_value: Decimal
    unrealised_pnl: Decimal
    locked: Decimal


@dataclass(frozen=True)
class WalletSnapshot:
    account_type: str
    total_equity: Decimal
    total_wallet_balance: Decimal
    total_available_balance: Decimal
    total_perp_upl: Decimal
    coins: tuple[CoinBalance, ...]

    def equity_for(self, settle_coin: str = "USDT") -> Decimal:
        """Prefer the settle-coin equity; fall back to unified totalEquity."""
        settle = settle_coin.strip().upper()
        for coin in self.coins:
            if coin.coin.upper() == settle:
                return coin.equity
        return self.total_equity


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    side: str
    size: Decimal
    avg_price: Decimal
    leverage: Decimal | None
    unrealised_pnl: Decimal | None
    liq_price: Decimal | None
    stop_loss: str
    take_profit: str
    position_idx: int


@dataclass(frozen=True)
class OpenOrderSnapshot:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    price: Decimal | None
    qty: Decimal
    status: str
    reduce_only: bool
    stop_order_type: str


@dataclass(frozen=True)
class ApiKeySnapshot:
    note: str
    read_only: bool
    uta: bool
    ips: tuple[str, ...]
    wallet_permissions: tuple[str, ...]
    has_withdraw: bool


@dataclass(frozen=True)
class AccountSnapshot:
    """Exchange is the source of truth. Local caches must be replaced by this on startup."""

    wallet: WalletSnapshot
    positions: tuple[PositionSnapshot, ...]
    open_orders: tuple[OpenOrderSnapshot, ...]
    api_key: ApiKeySnapshot
    fee_rates: dict[str, Any] = field(default_factory=dict)


def _parse_wallet(payload: dict[str, Any]) -> WalletSnapshot:
    rows = (payload.get("result") or {}).get("list") or []
    if not rows:
        return WalletSnapshot(
            account_type="",
            total_equity=Decimal("0"),
            total_wallet_balance=Decimal("0"),
            total_available_balance=Decimal("0"),
            total_perp_upl=Decimal("0"),
            coins=(),
        )
    row = rows[0]
    coins = tuple(
        CoinBalance(
            coin=str(item.get("coin") or ""),
            equity=to_decimal(item.get("equity") or "0"),
            wallet_balance=to_decimal(item.get("walletBalance") or "0"),
            usd_value=to_decimal(item.get("usdValue") or "0"),
            unrealised_pnl=to_decimal(item.get("unrealisedPnl") or "0"),
            locked=to_decimal(item.get("locked") or "0"),
        )
        for item in row.get("coin") or []
    )
    return WalletSnapshot(
        account_type=str(row.get("accountType") or ""),
        total_equity=to_decimal(row.get("totalEquity") or "0"),
        total_wallet_balance=to_decimal(row.get("totalWalletBalance") or "0"),
        total_available_balance=to_decimal(row.get("totalAvailableBalance") or "0"),
        total_perp_upl=to_decimal(row.get("totalPerpUPL") or "0"),
        coins=coins,
    )


def _parse_positions(payload: dict[str, Any]) -> tuple[PositionSnapshot, ...]:
    rows = (payload.get("result") or {}).get("list") or []
    positions = []
    for item in rows:
        size = to_decimal(item.get("size") or "0")
        if size == 0:
            continue
        positions.append(
            PositionSnapshot(
                symbol=str(item.get("symbol") or ""),
                side=str(item.get("side") or ""),
                size=size,
                avg_price=to_decimal(item.get("avgPrice") or "0"),
                leverage=optional_decimal(item.get("leverage")),
                unrealised_pnl=optional_decimal(item.get("unrealisedPnl")),
                liq_price=optional_decimal(item.get("liqPrice")),
                stop_loss=str(item.get("stopLoss") or ""),
                take_profit=str(item.get("takeProfit") or ""),
                position_idx=int(item.get("positionIdx") or 0),
            )
        )
    return tuple(positions)


def _parse_orders(payload: dict[str, Any]) -> tuple[OpenOrderSnapshot, ...]:
    rows = (payload.get("result") or {}).get("list") or []
    return tuple(
        OpenOrderSnapshot(
            order_id=str(item.get("orderId") or ""),
            client_order_id=str(item.get("orderLinkId") or ""),
            symbol=str(item.get("symbol") or ""),
            side=str(item.get("side") or ""),
            order_type=str(item.get("orderType") or ""),
            price=optional_decimal(item.get("price")),
            qty=to_decimal(item.get("qty") or "0"),
            status=str(item.get("orderStatus") or ""),
            reduce_only=bool(item.get("reduceOnly")),
            stop_order_type=str(item.get("stopOrderType") or ""),
        )
        for item in rows
    )


def has_protective_sl(position: PositionSnapshot) -> bool:
    sl = (position.stop_loss or "").strip()
    if not sl or sl in {"0", "0.0", "0.00"}:
        return False
    try:
        return to_decimal(sl) > 0
    except Exception:  # noqa: BLE001
        return False


def parse_ws_position_message(payload: dict[str, Any]) -> tuple[PositionSnapshot, ...]:
    topic = str(payload.get("topic") or "")
    if topic != "position" and not topic.startswith("position."):
        return ()
    rows = payload.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    return _parse_positions({"result": {"list": rows}})


def _parse_api_key(result: dict[str, Any]) -> ApiKeySnapshot:
    wallet = tuple(str(item) for item in (result.get("permissions") or {}).get("Wallet") or [])
    return ApiKeySnapshot(
        note=str(result.get("note") or ""),
        read_only=bool(result.get("readOnly")),
        uta=bool(result.get("uta")),
        ips=tuple(str(item) for item in result.get("ips") or []),
        wallet_permissions=wallet,
        has_withdraw=any(item.lower() == "withdraw" for item in wallet),
    )


class AccountService:
    def __init__(self, client: BybitRESTClient) -> None:
        self._client = client

    def snapshot(self, *, symbol: str | None = None) -> AccountSnapshot:
        api_key_raw = self._client.assert_safe_api_key()
        return AccountSnapshot(
            wallet=self.wallet(),
            positions=self.positions(symbol),
            open_orders=self.open_orders(symbol),
            api_key=_parse_api_key(api_key_raw),
            fee_rates=(self._client.get_fee_rates(symbol).get("result") or {}),
        )

    def wallet(self) -> WalletSnapshot:
        """Wallet only. Does not re-check API-key permissions (snapshot() does that once)."""
        return _parse_wallet(self._client.get_wallet_balance())

    def positions(self, symbol: str | None = None) -> tuple[PositionSnapshot, ...]:
        return _parse_positions(self._client.get_positions(symbol))

    def open_orders(self, symbol: str | None = None) -> tuple[OpenOrderSnapshot, ...]:
        return _parse_orders(self._client.get_open_orders(symbol))
