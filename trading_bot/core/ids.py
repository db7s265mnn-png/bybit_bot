from __future__ import annotations

import uuid


def new_event_id() -> str:
    """Unique id for a log event / trade lifecycle step. Never a secret."""
    return uuid.uuid4().hex
