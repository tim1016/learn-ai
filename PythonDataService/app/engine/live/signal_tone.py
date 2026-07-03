"""Backend-authored tone projection for live decision signals."""

from __future__ import annotations

from collections.abc import Mapping

from app.engine.live.artifacts import SIGNAL_VALUES
from app.schemas.live_runs import SignalTone

_SIGNAL_TONE_BY_SIGNAL: dict[str, SignalTone] = {
    "ENTER": "ok",
    "EXIT": "warn",
    "HOLD": "neutral",
}

if set(_SIGNAL_TONE_BY_SIGNAL) != SIGNAL_VALUES:  # pragma: no cover - import-time contract guard
    raise RuntimeError("Signal tone vocabulary must match app.engine.live.artifacts.SIGNAL_VALUES")


def latest_signal_tone(latest_decision: Mapping[str, object] | None) -> SignalTone:
    """Project the latest canonical live decision signal into UI tone."""

    signal = latest_decision.get("signal") if latest_decision is not None else None
    if not isinstance(signal, str):
        return "neutral"
    return _SIGNAL_TONE_BY_SIGNAL.get(signal.upper(), "neutral")


__all__ = ["latest_signal_tone"]
