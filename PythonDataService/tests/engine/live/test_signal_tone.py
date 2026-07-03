"""Tests for canonical live decision signal tone projection."""

from __future__ import annotations

import pytest

from app.engine.live.signal_tone import latest_signal_tone


@pytest.mark.parametrize(
    ("signal", "tone"),
    [
        ("ENTER", "ok"),
        ("EXIT", "warn"),
        ("HOLD", "neutral"),
        ("BUY", "neutral"),
        ("SELL", "neutral"),
        (None, "neutral"),
    ],
)
def test_latest_signal_tone_uses_canonical_signal_vocabulary(signal: object, tone: str) -> None:
    latest_decision = {"signal": signal} if signal is not None else None

    assert latest_signal_tone(latest_decision) == tone
