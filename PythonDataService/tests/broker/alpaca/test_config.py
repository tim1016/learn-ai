"""Tests for AlpacaSettings — paper-only safety and URL derivation (spec §7)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk import (
    AlpacaClerk,
    get_alpaca_clerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.config import (
    AlpacaSettings,
    reset_alpaca_settings_for_testing,
)
from app.main import _alpaca_clerk_configuration_is_valid


def test_paper_mode_derives_paper_base_url() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")

    assert settings.is_paper is True
    assert settings.base_url == "https://paper-api.alpaca.markets"


def test_mode_defaults_to_paper() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s")

    assert settings.mode == "paper"


def test_live_mode_is_refused() -> None:
    with pytest.raises(ValidationError, match="ALPACA_MODE must be 'paper'"):
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live")


def test_missing_credentials_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)

    with pytest.raises(ValidationError):
        AlpacaSettings(_env_file=None)


def test_invalid_configuration_clears_stale_clerk_without_logging_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "must-not-appear-in-logs"
    set_alpaca_clerk(MagicMock(spec=AlpacaClerk))
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", secret)
    monkeypatch.setenv("ALPACA_MODE", "live")
    reset_alpaca_settings_for_testing()

    try:
        with caplog.at_level("WARNING"):
            assert _alpaca_clerk_configuration_is_valid() is False

        assert get_alpaca_clerk() is None
        rendered = " ".join(
            f"{record.getMessage()} {getattr(record, 'detail', '')}"
            for record in caplog.records
        )
        assert "ALPACA_MODE must be 'paper'" in rendered
        assert secret not in rendered
    finally:
        reset_alpaca_clerk_for_testing()
        reset_alpaca_settings_for_testing()
