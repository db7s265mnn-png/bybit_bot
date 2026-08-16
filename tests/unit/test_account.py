from __future__ import annotations

from trading_bot.account.service import _parse_api_key, _parse_wallet, _parse_positions
from trading_bot.core.exceptions import WithdrawPermissionError
from trading_bot.exchange.bybit_client import BybitRESTClient


def test_wallet_parser_reads_unified_account() -> None:
    payload = {
        "result": {
            "list": [
                {
                    "accountType": "UNIFIED",
                    "totalEquity": "10000",
                    "totalWalletBalance": "10000",
                    "totalAvailableBalance": "9000",
                    "totalPerpUPL": "-10",
                    "coin": [
                        {
                            "coin": "USDT",
                            "equity": "10000",
                            "walletBalance": "10000",
                            "usdValue": "10000",
                            "unrealisedPnl": "-10",
                            "locked": "0",
                        }
                    ],
                }
            ]
        }
    }
    wallet = _parse_wallet(payload)
    assert wallet.total_equity == 10000
    assert wallet.coins[0].coin == "USDT"


def test_zero_size_positions_are_ignored() -> None:
    payload = {
        "result": {
            "list": [
                {"symbol": "BTCUSDT", "side": "Buy", "size": "0", "avgPrice": "0"},
                {"symbol": "ETHUSDT", "side": "Sell", "size": "0.2", "avgPrice": "2000"},
            ]
        }
    }
    positions = _parse_positions(payload)
    assert len(positions) == 1
    assert positions[0].symbol == "ETHUSDT"


def test_withdraw_permission_detected() -> None:
    snap = _parse_api_key(
        {
            "note": "bot",
            "readOnly": 0,
            "uta": 1,
            "ips": ["1.1.1.1"],
            "permissions": {"Wallet": ["AccountTransfer", "Withdraw"]},
        }
    )
    assert snap.has_withdraw is True


def test_assert_safe_api_key_rejects_withdraw(app_config, monkeypatch) -> None:
    client = BybitRESTClient(app_config)

    def fake_info() -> dict:
        return {
            "retCode": 0,
            "result": {
                "note": "unsafe",
                "readOnly": 0,
                "permissions": {"Wallet": ["Withdraw"]},
                "ips": ["127.0.0.1"],
            },
        }

    monkeypatch.setattr(client, "get_api_key_information", fake_info)
    try:
        client.assert_safe_api_key()
        raise AssertionError("expected withdraw rejection")
    except WithdrawPermissionError:
        pass
