"""Fault injection never arms under live settings (ADR 0059 D10; slice 7 R14 verifies, changes nothing)."""

from __future__ import annotations

import pytest

from app.broker.alpaca import fault_injection
from tests.broker.alpaca.clerk.live_arming_fixtures import live_settings


def test_injection_is_refused_under_live_settings_even_with_the_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fault_injection.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fault_injection, "get_alpaca_settings", live_settings)
    assert fault_injection.injection_permitted() is False
