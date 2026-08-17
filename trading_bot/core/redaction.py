from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|authorization|api[_-]?key|api[_-]?secret|x-bapi-sign)$",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)

REDACTED = "***REDACTED***"


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key)))


def redact_value(_key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value:
        return value
    return REDACTED


def redact_mapping(data: Mapping[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if is_secret_key(str(key)):
            redacted[str(key)] = REDACTED
        else:
            redacted[str(key)] = redact_obj(value)
    return redacted


def redact_obj(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, str):
        return _BEARER_RE.sub(rf"\1{REDACTED}", value)
    return value


def redact_text(text: str, extra_secrets: list[str] | None = None) -> str:
    redacted = _BEARER_RE.sub(rf"\1{REDACTED}", text)
    for secret in extra_secrets or []:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, REDACTED)
    return redacted
