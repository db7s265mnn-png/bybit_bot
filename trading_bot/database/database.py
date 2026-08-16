from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from trading_bot.core.ids import new_event_id
from trading_bot.exchange.instruments import Instrument, parse_instrument, to_decimal
from trading_bot.market.candles import Candle

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _row_str(row: sqlite3.Row, key: str, default: str = "") -> str:
    keys = row.keys()
    if key not in keys:
        return default
    value = row[key]
    if value is None:
        return default
    return str(value)


def _row_json(row: sqlite3.Row, key: str) -> dict[str, Any] | None:
    raw = _row_str(row, key, "")
    if not raw:
        return None
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None


def sqlite_path_from_url(url: str) -> str:
    """Translate SQLAlchemy-style sqlite URLs. PostgreSQL URLs are rejected until an adapter exists."""
    if url.startswith("postgres"):
        raise NotImplementedError(
            "PostgreSQL is not wired yet. Keep using sqlite:///... ; the repository SQL is portable."
        )
    if not url.startswith("sqlite:"):
        raise ValueError(f"unsupported database url: {url}")
    if url in {"sqlite://", "sqlite:///:memory:"}:
        return ":memory:"
    parsed = urlparse(url)
    path = parsed.path or ""
    if url.startswith("sqlite:////"):
        return "/" + url.split("sqlite:////", 1)[1]
    relative = path.lstrip("/")
    resolved = Path(relative)
    if not resolved.is_absolute():
        resolved = _PROJECT_ROOT / resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS candles (
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        start_ms INTEGER NOT NULL,
        open TEXT NOT NULL,
        high TEXT NOT NULL,
        low TEXT NOT NULL,
        close TEXT NOT NULL,
        volume TEXT NOT NULL,
        turnover TEXT NOT NULL,
        confirmed INTEGER NOT NULL,
        PRIMARY KEY (symbol, interval, start_ms)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instruments (
        category TEXT NOT NULL,
        symbol TEXT NOT NULL,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (category, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        trade_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        entry_price TEXT NOT NULL,
        exit_price TEXT,
        quantity TEXT NOT NULL,
        entry_time TEXT NOT NULL,
        exit_time TEXT,
        gross_pnl TEXT,
        fees TEXT,
        funding TEXT,
        slippage TEXT,
        net_pnl TEXT,
        strategy TEXT NOT NULL,
        stop_loss TEXT,
        take_profit TEXT,
        session_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        client_order_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        type TEXT NOT NULL,
        price TEXT,
        quantity TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        session_id TEXT,
        payload TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_events (
        event_id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        level TEXT NOT NULL,
        event TEXT NOT NULL,
        message TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_accounts (
        session_id TEXT PRIMARY KEY,
        equity TEXT NOT NULL,
        initial_balance TEXT NOT NULL,
        last_candles_json TEXT,
        consecutive_losses INTEGER NOT NULL DEFAULT 0,
        day_key TEXT,
        day_start_equity TEXT,
        daily_realized TEXT,
        pending_json TEXT,
        strategy TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval ON candles(symbol, interval, start_ms)",
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol_time ON trades(symbol, entry_time)",
    "CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_time ON bot_events(timestamp)",
]


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    symbol: str
    side: str
    entry_price: Decimal
    exit_price: Decimal | None
    quantity: Decimal
    entry_time: datetime
    exit_time: datetime | None
    gross_pnl: Decimal | None
    fees: Decimal | None
    funding: Decimal | None
    slippage: Decimal | None
    net_pnl: Decimal | None
    strategy: str
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    session_id: str = ""


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    type: str
    price: Decimal | None
    quantity: Decimal
    status: str
    created_at: datetime
    updated_at: datetime
    session_id: str = ""
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class PaperAccount:
    session_id: str
    equity: Decimal
    initial_balance: Decimal
    last_candles: dict[str, int]
    consecutive_losses: int
    day_key: str
    day_start_equity: Decimal
    daily_realized: Decimal
    pending_json: str
    strategy: str
    updated_at: datetime


@dataclass(frozen=True)
class BotEvent:
    timestamp: datetime
    level: str
    event: str
    message: str
    event_id: str


class Database:
    """SQLite repository. SQL is standard enough to port to PostgreSQL later."""

    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        self.url = url
        self._path = sqlite_path_from_url(url)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        with self._conn:
            for statement in SCHEMA:
                self._conn.execute(statement)
            self._ensure_column("trades", "session_id", "TEXT")
            self._ensure_column("orders", "session_id", "TEXT")
            self._ensure_column("orders", "payload", "TEXT")

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {str(row[1]) for row in rows}
        if column not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def upsert_candles(self, candles: Iterable[Candle]) -> int:
        rows = [
            (
                c.symbol,
                c.interval,
                c.start_ms,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
                str(c.turnover),
                1 if c.confirmed else 0,
            )
            for c in candles
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO candles(
                    symbol, interval, start_ms, open, high, low, close, volume, turnover, confirmed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, start_ms) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    turnover=excluded.turnover,
                    confirmed=excluded.confirmed
                """,
                rows,
            )
        return len(rows)

    def load_candles(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        confirmed_only: bool = True,
    ) -> list[Candle]:
        sql = "SELECT * FROM candles WHERE symbol=? AND interval=?"
        params: list[Any] = [symbol, interval]
        if start_ms is not None:
            sql += " AND start_ms>=?"
            params.append(start_ms)
        if end_ms is not None:
            sql += " AND start_ms<=?"
            params.append(end_ms)
        if confirmed_only:
            sql += " AND confirmed=1"
        sql += " ORDER BY start_ms ASC"
        out: list[Candle] = []
        for row in self._conn.execute(sql, params):
            out.append(
                Candle(
                    symbol=row["symbol"],
                    interval=row["interval"],
                    start_time=datetime.fromtimestamp(row["start_ms"] / 1000, tz=timezone.utc),
                    open=to_decimal(row["open"]),
                    high=to_decimal(row["high"]),
                    low=to_decimal(row["low"]),
                    close=to_decimal(row["close"]),
                    volume=to_decimal(row["volume"]),
                    turnover=to_decimal(row["turnover"]),
                    confirmed=bool(row["confirmed"]),
                )
            )
        return out

    def save_instrument(self, instrument: Instrument) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO instruments(category, symbol, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(category, symbol) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (
                    instrument.category,
                    instrument.symbol,
                    json.dumps(instrument.raw),
                    _utc_iso(datetime.now(timezone.utc)),
                ),
            )

    def load_instrument(self, category: str, symbol: str) -> Instrument | None:
        row = self._conn.execute(
            "SELECT payload FROM instruments WHERE category=? AND symbol=?",
            (category, symbol),
        ).fetchone()
        if row is None:
            return None
        return parse_instrument(category, json.loads(row["payload"]))

    def upsert_trade(self, trade: TradeRecord) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO trades(
                    trade_id, symbol, side, entry_price, exit_price, quantity,
                    entry_time, exit_time, gross_pnl, fees, funding, slippage,
                    net_pnl, strategy, stop_loss, take_profit, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    exit_price=excluded.exit_price,
                    exit_time=excluded.exit_time,
                    gross_pnl=excluded.gross_pnl,
                    fees=excluded.fees,
                    funding=excluded.funding,
                    slippage=excluded.slippage,
                    net_pnl=excluded.net_pnl,
                    stop_loss=excluded.stop_loss,
                    take_profit=excluded.take_profit,
                    session_id=COALESCE(excluded.session_id, trades.session_id)
                """,
                (
                    trade.trade_id,
                    trade.symbol,
                    trade.side,
                    str(trade.entry_price),
                    None if trade.exit_price is None else str(trade.exit_price),
                    str(trade.quantity),
                    _utc_iso(trade.entry_time),
                    _utc_iso(trade.exit_time),
                    None if trade.gross_pnl is None else str(trade.gross_pnl),
                    None if trade.fees is None else str(trade.fees),
                    None if trade.funding is None else str(trade.funding),
                    None if trade.slippage is None else str(trade.slippage),
                    None if trade.net_pnl is None else str(trade.net_pnl),
                    trade.strategy,
                    None if trade.stop_loss is None else str(trade.stop_loss),
                    None if trade.take_profit is None else str(trade.take_profit),
                    trade.session_id or None,
                ),
            )

    def list_trades(
        self,
        symbol: str | None = None,
        *,
        session_id: str | None = None,
        open_only: bool = False,
    ) -> list[TradeRecord]:
        sql = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if session_id is not None:
            sql += " AND IFNULL(session_id,'')=?"
            params.append(session_id)
        if open_only:
            sql += " AND exit_price IS NULL"
        sql += " ORDER BY entry_time ASC"
        return [self._trade_from_row(row) for row in self._conn.execute(sql, params)]

    def upsert_order(self, order: OrderRecord) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO orders(
                    order_id, client_order_id, symbol, side, type, price, quantity,
                    status, created_at, updated_at, session_id, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status=excluded.status,
                    price=excluded.price,
                    quantity=excluded.quantity,
                    updated_at=excluded.updated_at,
                    session_id=COALESCE(excluded.session_id, orders.session_id),
                    payload=COALESCE(excluded.payload, orders.payload)
                """,
                (
                    order.order_id,
                    order.client_order_id,
                    order.symbol,
                    order.side,
                    order.type,
                    None if order.price is None else str(order.price),
                    str(order.quantity),
                    order.status,
                    _utc_iso(order.created_at),
                    _utc_iso(order.updated_at),
                    order.session_id or None,
                    None if not order.payload else json.dumps(order.payload, default=str),
                ),
            )

    def get_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id=? ORDER BY created_at DESC LIMIT 1",
            (client_order_id,),
        ).fetchone()
        return None if row is None else self._order_from_row(row)

    def list_orders(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
    ) -> list[OrderRecord]:
        sql = "SELECT * FROM orders WHERE 1=1"
        params: list[Any] = []
        if session_id is not None:
            sql += " AND IFNULL(session_id,'')=?"
            params.append(session_id)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY created_at ASC"
        return [self._order_from_row(row) for row in self._conn.execute(sql, params)]

    def save_paper_account(self, account: PaperAccount) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO paper_accounts(
                    session_id, equity, initial_balance, last_candles_json,
                    consecutive_losses, day_key, day_start_equity, daily_realized,
                    pending_json, strategy, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    equity=excluded.equity,
                    initial_balance=excluded.initial_balance,
                    last_candles_json=excluded.last_candles_json,
                    consecutive_losses=excluded.consecutive_losses,
                    day_key=excluded.day_key,
                    day_start_equity=excluded.day_start_equity,
                    daily_realized=excluded.daily_realized,
                    pending_json=excluded.pending_json,
                    strategy=excluded.strategy,
                    updated_at=excluded.updated_at
                """,
                (
                    account.session_id,
                    str(account.equity),
                    str(account.initial_balance),
                    json.dumps(account.last_candles),
                    account.consecutive_losses,
                    account.day_key,
                    str(account.day_start_equity),
                    str(account.daily_realized),
                    account.pending_json,
                    account.strategy,
                    _utc_iso(account.updated_at),
                ),
            )

    def load_paper_account(self, session_id: str) -> PaperAccount | None:
        row = self._conn.execute(
            "SELECT * FROM paper_accounts WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        raw_last = row["last_candles_json"]
        last = json.loads(raw_last) if raw_last else {}
        last_candles = {str(k): int(v) for k, v in last.items()}
        return PaperAccount(
            session_id=row["session_id"],
            equity=to_decimal(row["equity"]),
            initial_balance=to_decimal(row["initial_balance"]),
            last_candles=last_candles,
            consecutive_losses=int(row["consecutive_losses"] or 0),
            day_key=row["day_key"] or "",
            day_start_equity=to_decimal(row["day_start_equity"] or "0"),
            daily_realized=to_decimal(row["daily_realized"] or "0"),
            pending_json=row["pending_json"] or "",
            strategy=row["strategy"],
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def record_event(
        self,
        event: str,
        message: str,
        *,
        level: str = "INFO",
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> BotEvent:
        record = BotEvent(
            timestamp=timestamp or datetime.now(timezone.utc),
            level=level.upper(),
            event=event,
            message=message,
            event_id=event_id or new_event_id(),
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO bot_events(event_id, timestamp, level, event, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.event_id, _utc_iso(record.timestamp), record.level, record.event, record.message),
            )
        return record

    def list_events(self, limit: int = 100) -> list[BotEvent]:
        rows = self._conn.execute(
            "SELECT * FROM bot_events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        return [
            BotEvent(
                timestamp=_parse_dt(row["timestamp"]) or datetime.now(timezone.utc),
                level=row["level"],
                event=row["event"],
                message=row["message"],
                event_id=row["event_id"],
            )
            for row in rows
        ]

    @staticmethod
    def _trade_from_row(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            trade_id=row["trade_id"],
            symbol=row["symbol"],
            side=row["side"],
            entry_price=to_decimal(row["entry_price"]),
            exit_price=None if row["exit_price"] is None else to_decimal(row["exit_price"]),
            quantity=to_decimal(row["quantity"]),
            entry_time=_parse_dt(row["entry_time"]) or datetime.now(timezone.utc),
            exit_time=_parse_dt(row["exit_time"]),
            gross_pnl=None if row["gross_pnl"] is None else to_decimal(row["gross_pnl"]),
            fees=None if row["fees"] is None else to_decimal(row["fees"]),
            funding=None if row["funding"] is None else to_decimal(row["funding"]),
            slippage=None if row["slippage"] is None else to_decimal(row["slippage"]),
            net_pnl=None if row["net_pnl"] is None else to_decimal(row["net_pnl"]),
            strategy=row["strategy"],
            stop_loss=None if row["stop_loss"] is None else to_decimal(row["stop_loss"]),
            take_profit=None if row["take_profit"] is None else to_decimal(row["take_profit"]),
            session_id=_row_str(row, "session_id"),
        )

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            order_id=row["order_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            type=row["type"],
            price=None if row["price"] is None else to_decimal(row["price"]),
            quantity=to_decimal(row["quantity"]),
            status=row["status"],
            created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
            session_id=_row_str(row, "session_id"),
            payload=_row_json(row, "payload"),
        )
