from __future__ import annotations

from trading_bot.core.redaction import REDACTED, redact_mapping, redact_text


def test_redact_mapping_hides_secret_keys() -> None:
    payload = {
        "api_key": "public-id",
        "api_secret": "should-not-appear",
        "nested": {"telegram_bot_token": "123:abc", "ok": True},
    }
    redacted = redact_mapping(payload)
    assert redacted["api_secret"] == REDACTED
    assert redacted["nested"]["telegram_bot_token"] == REDACTED
    assert redacted["nested"]["ok"] is True
    assert redacted["api_key"] == REDACTED


def test_redact_text_replaces_known_secrets() -> None:
    text = "signed with super-secret-value and Bearer abc.def"
    assert "super-secret-value" not in redact_text(text, extra_secrets=["super-secret-value"])
    assert REDACTED in redact_text(text, extra_secrets=["super-secret-value"])
