from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

from trading_bot.config.models import KillSwitchPolicy
from trading_bot.core.exceptions import KillSwitchActiveError
from trading_bot.core.ids import new_event_id


@dataclass(frozen=True)
class KillSwitchState:
    active: bool
    reason: str
    policy: KillSwitchPolicy
    event_id: str
    activated_at: datetime | None


class KillSwitch:
    """Emergency halt. New entries/orders must check this before sending.

    Paper trading honors the policy: hold keeps virtual positions (SL/TP still apply);
    flatten closes them at the next simulated market price. Live flatten uses
    OrderManager.reduce-only market close (Phase 7).
    """

    def __init__(self, policy: KillSwitchPolicy = KillSwitchPolicy.HOLD) -> None:
        self._lock = RLock()
        self._policy = policy
        self._active = False
        self._reason = ""
        self._event_id = ""
        self._activated_at: datetime | None = None

    def activate(self, reason: str) -> KillSwitchState:
        with self._lock:
            if not self._active:
                self._active = True
                self._reason = reason
                self._event_id = new_event_id()
                self._activated_at = datetime.now(timezone.utc)
            return self.snapshot()

    def reset(self) -> None:
        with self._lock:
            self._active = False
            self._reason = ""
            self._event_id = ""
            self._activated_at = None

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def assert_allows_new_orders(self) -> None:
        with self._lock:
            if self._active:
                raise KillSwitchActiveError(
                    f"kill switch active (event_id={self._event_id}): {self._reason}"
                )

    def snapshot(self) -> KillSwitchState:
        with self._lock:
            return KillSwitchState(
                active=self._active,
                reason=self._reason,
                policy=self._policy,
                event_id=self._event_id,
                activated_at=self._activated_at,
            )
