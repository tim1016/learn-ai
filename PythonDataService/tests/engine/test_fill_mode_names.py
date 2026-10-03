"""Tests for app.engine.execution.fill_mode_names: one name, one mode, on every surface (#2599)."""

from __future__ import annotations

import pytest

from app.engine.execution.fill_mode_names import REQUEST_FILL_MODES, UnknownFillModeError, parse_fill_mode
from app.engine.execution.order import FillMode


@pytest.mark.parametrize("mode", list(FillMode))
def test_every_mode_parses_from_its_own_value(mode: FillMode) -> None:
    assert parse_fill_mode(mode.value) is mode


@pytest.mark.parametrize(
    ("spelling", "mode"),
    [
        ("close", FillMode.SIGNAL_BAR_CLOSE),
        ("SignalBarClose", FillMode.SIGNAL_BAR_CLOSE),
        ("open", FillMode.NEXT_BAR_OPEN),
        ("nextbaropen", FillMode.NEXT_BAR_OPEN),
        ("SIGNAL-BAR-CLOSE", FillMode.SIGNAL_BAR_CLOSE),
        ("Next-Session-Open", FillMode.NEXT_SESSION_OPEN),
        (" DECISION_MINUTE_OPEN ", FillMode.DECISION_MINUTE_OPEN),
        ("decision-minute-open", FillMode.DECISION_MINUTE_OPEN),
    ],
)
def test_aliases_case_space_and_hyphens_name_the_same_mode(spelling: str, mode: FillMode) -> None:
    assert parse_fill_mode(spelling) is mode


def test_an_unknown_name_is_refused_with_the_modes_the_surface_offers() -> None:
    with pytest.raises(UnknownFillModeError) as refused:
        parse_fill_mode("magic", allowed=REQUEST_FILL_MODES)

    assert refused.value.raw == "magic"
    assert refused.value.allowed == REQUEST_FILL_MODES
    assert str(refused.value) == (
        "unknown fill_mode 'magic' — expected signal_bar_close, next_bar_open or decision_minute_open"
    )


def test_a_real_mode_the_surface_does_not_offer_is_refused() -> None:
    assert parse_fill_mode("next_session_open") is FillMode.NEXT_SESSION_OPEN
    with pytest.raises(UnknownFillModeError) as refused:
        parse_fill_mode("next_session_open", allowed=REQUEST_FILL_MODES)

    assert "next_session_open" not in refused.value.expected
