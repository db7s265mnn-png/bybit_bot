from __future__ import annotations

import pytest

from trading_bot.exchange.websocket import BybitWebSocket, StreamKind, ticker_topic
from trading_bot.core.types import ConnectionState


@pytest.mark.integration
@pytest.mark.timeout(40)
def test_public_ticker_stream(app_config, require_bybit) -> None:
    messages: list[dict] = []

    def on_message(payload: dict) -> None:
        if payload.get("topic"):
            messages.append(payload)

    ws = BybitWebSocket(app_config, kind=StreamKind.PUBLIC, on_message=on_message)
    ws.subscribe([ticker_topic("BTCUSDT")])
    ws.start()
    try:
        ws.wait_connected(timeout=20)
        deadline_ok = False
        for _ in range(100):
            if messages:
                deadline_ok = True
                break
            import time

            time.sleep(0.1)
        assert ws.state is ConnectionState.CONNECTED
        assert deadline_ok, "did not receive a public ticker message"
        assert ws.trading_paused is False
    finally:
        ws.stop()
        assert ws.state is ConnectionState.STOPPED
