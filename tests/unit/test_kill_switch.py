from __future__ import annotations

from trading_bot.core.exceptions import KillSwitchActiveError
from trading_bot.core.kill_switch import KillSwitch


def test_kill_switch_blocks_new_orders() -> None:
    switch = KillSwitch()
    assert switch.is_active() is False
    state = switch.activate("manual test")
    assert state.active is True
    assert state.event_id
    try:
        switch.assert_allows_new_orders()
        raise AssertionError("kill switch should block")
    except KillSwitchActiveError as exc:
        assert "manual test" in str(exc)
