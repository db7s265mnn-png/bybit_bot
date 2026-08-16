from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from trading_bot.core.ids import new_event_id
from trading_bot.core.redaction import REDACTED, is_secret_key, redact_text

_SECRET_EVENT_KEYS = {
    "api_secret",
    "bybit_api_secret",
    "telegram_bot_token",
    "telegram_token",
    "password",
    "secret",
    "token",
    "authorization",
}


def _drop_secrets(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if is_secret_key(key) or key.lower() in _SECRET_EVENT_KEYS:
            event_dict[key] = REDACTED
        elif isinstance(event_dict[key], str):
            event_dict[key] = redact_text(event_dict[key])
    return event_dict


def _ensure_event_id(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict.setdefault("event_id", new_event_id())
    return event_dict


def configure_logging(
    level: str = "INFO",
    *,
    json_logs: bool = False,
    log_dir: str | Path | None = "logs",
) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric,
        force=True,
    )
    logging.getLogger("pybit").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _ensure_event_id,
        _drop_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)


def get_logger(name: str = "trading_bot") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
