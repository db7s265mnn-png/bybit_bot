from __future__ import annotations

import uuid


def new_event_id() -> str:
    """Unique id for a log event / trade lifecycle step. Never a secret."""
    return uuid.uuid4().hex


def new_order_link_id(prefix: str = "b") -> str:
    """Bybit orderLinkId: letters/numbers, max 36 chars. Used for idempotent retries."""
    cleaned = "".join(ch for ch in prefix if ch.isalnum())[:8] or "b"
    return f"{cleaned}{uuid.uuid4().hex}"[:36]
