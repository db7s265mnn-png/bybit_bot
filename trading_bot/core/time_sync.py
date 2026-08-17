from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TimeSync:
    """Offset = server_ms - local_ms. Positive means local clock is behind."""

    offset_ms: int = 0
    server_time_ms: int = 0
    local_time_ms: int = 0

    @property
    def skew_too_large(self) -> bool:
        return abs(self.offset_ms) > 2000

    def now_ms(self) -> int:
        return int(datetime.now(timezone.utc).timestamp() * 1000) + self.offset_ms
