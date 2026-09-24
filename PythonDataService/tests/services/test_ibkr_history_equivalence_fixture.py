"""The receipt behind #2314's hole fill: IBKR 1-minute history vs live-assembled minutes.

See ``tests/fixtures/golden/ibkr-history-vs-live-minutes-2026-09-24/attribution.md``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "golden" / "ibkr-history-vs-live-minutes-2026-09-24"
_FIELDS = ("open", "high", "low", "close")


def _load(name: str) -> dict:
    return json.loads((_FIXTURE / name).read_text())


def _mismatches(phase_is_rth: bool) -> tuple[int, int]:
    history = {bar["start_ms"]: bar for bar in _load("history_minutes.json")["bars"]}
    live = [bar for bar in _load("live_minutes.json")["bars"] if (bar["session_phase"] == "RTH") == phase_is_rth]
    differing = sum(
        1
        for bar in live
        if any(Decimal(bar[field]) != Decimal(history[bar["start_ms"]][field]) for field in _FIELDS)
        or bar["volume"] != history[bar["start_ms"]]["volume"]
    )
    return differing, len(live)


def test_history_reproduces_every_live_regular_hours_minute_exactly() -> None:
    assert _mismatches(phase_is_rth=True) == (0, 388)


def test_history_does_not_reproduce_every_extended_hours_minute() -> None:
    """Why a hole holding an after-hours minute the run decides on is refused."""
    assert _mismatches(phase_is_rth=False) == (20, 69)


def test_history_returns_every_regular_minute_of_a_thin_symbol() -> None:
    """Why requiring every owed regular minute cannot lock a thin symbol out."""
    symbols = _load("thin_symbol_completeness.json")["symbols"]
    assert {counts["rth_minutes"] for counts in symbols.values()} == {390}
    assert symbols["KBWP"]["zero_volume"] > 0
