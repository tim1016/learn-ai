"""Tests for AlpacaSettings — paper-only safety and URL derivation (spec §7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.broker.alpaca.config import AlpacaSettings


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
