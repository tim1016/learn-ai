"""Tests for AlpacaSettings — mode agreement and URL derivation (spec §7)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk import (
    get_alpaca_clerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.config import (
    AlpacaSettings,
    reset_alpaca_settings_for_testing,
)

_LIVE_REQUIRED = {
    "live_loss_fraction": 0.02,
    "live_loss_usd": 500.0,
    "live_shadow_sessions": 5,
    "live_arming_max_sessions": 20,
    "live_xh_entry_bps": 10.0,
    "live_xh_exit_bps": 10.0,
}


def test_paper_mode_derives_paper_base_url() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")

    assert settings.is_paper is True
    assert settings.base_url == "https://paper-api.alpaca.markets"


def test_mode_defaults_to_paper() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s")

    assert settings.mode == "paper"


def test_live_mode_without_every_required_value_is_refused() -> None:
    with pytest.raises(ValidationError, match="ALPACA_MODE=live requires") as info:
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live")

    message = str(info.value)
    for name in (
        "ALPACA_LIVE_LOSS_FRACTION",
        "ALPACA_LIVE_LOSS_USD",
        "ALPACA_LIVE_SHADOW_SESSIONS",
        "ALPACA_LIVE_ARMING_MAX_SESSIONS",
        "ALPACA_LIVE_XH_ENTRY_BPS",
        "ALPACA_LIVE_XH_EXIT_BPS",
    ):
        assert name in message


def test_live_mode_names_only_the_missing_values() -> None:
    partial = dict(_LIVE_REQUIRED)
    partial.pop("live_arming_max_sessions")

    with pytest.raises(ValidationError, match="ALPACA_LIVE_ARMING_MAX_SESSIONS") as info:
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **partial)

    assert "ALPACA_LIVE_LOSS_FRACTION" not in str(info.value)


def test_live_mode_with_every_required_value_derives_live_base_url() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **_LIVE_REQUIRED)

    assert settings.is_live is True
    assert settings.is_paper is False
    assert settings.base_url == "https://api.alpaca.markets"


def test_paper_mode_ignores_live_values_entirely() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")

    assert settings.is_live is False
    assert settings.live_loss_fraction is None


@pytest.mark.parametrize(
    ("field", "bad"),
    [("live_loss_fraction", 0.0), ("live_loss_fraction", 1.0), ("live_loss_usd", 0.0),
     ("live_shadow_sessions", 0), ("live_arming_max_sessions", 0), ("live_xh_entry_bps", -1.0)],
)
def test_live_values_have_domain_bounds(field: str, bad: float | int) -> None:
    values = dict(_LIVE_REQUIRED)
    values[field] = bad

    with pytest.raises(ValidationError):
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **values)


def test_missing_credentials_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)

    with pytest.raises(ValidationError):
        AlpacaSettings(_env_file=None)


async def test_invalid_configuration_clears_stale_clerk_without_logging_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A half-edited ``.env`` on an installation with no profiles.

    Under ADR 0060 the startup probe resolves a *binding* rather than reading
    the environment, but this deployment shape — no profiles saved yet — still
    bootstraps from the environment, so the diagnostic that names the missing
    ``ALPACA_LIVE_*`` variables is still the true one and must survive. It
    belongs in the log, never in the operator-facing refusal, and the
    credential must not appear in either.
    """
    from app.main import _install_alpaca_binding

    secret = "must-not-appear-in-logs"
    set_alpaca_clerk(MagicMock())
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", secret)
    monkeypatch.setenv("ALPACA_MODE", "live")
    reset_alpaca_settings_for_testing()

    try:
        with caplog.at_level("WARNING"):
            assert await _install_alpaca_binding() is None

        assert get_alpaca_clerk() is None
        rendered = " ".join(
            f"{record.getMessage()} {getattr(record, 'detail', '')}"
            for record in caplog.records
        )
        assert "ALPACA_MODE=live requires" in rendered
        assert secret not in rendered
    finally:
        reset_alpaca_clerk_for_testing()
        reset_alpaca_settings_for_testing()


@pytest.mark.parametrize(
    "field", ["live_loss_fraction", "live_loss_usd", "live_xh_entry_bps", "live_xh_exit_bps"]
)
@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_live_values_must_be_finite(field: str, bad: float) -> None:
    # `inf` satisfies `gt=0` unless finiteness is required: an unbounded loss
    # limit would be an envelope in name only (ADR 0059 D4).
    values = dict(_LIVE_REQUIRED)
    values[field] = bad

    with pytest.raises(ValidationError):
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **values)
