"""Tests for app.engine.execution.fill_mode_names: one name, one mode, on every surface (#2599)."""

from __future__ import annotations

import pytest

from app.engine.execution.fill_mode_names import (
    ENGINE_BACKTEST_ALIASES,
    REQUEST_FILL_MODES,
    UnknownFillModeError,
    parse_fill_mode,
)
from app.engine.execution.order import FillMode


@pytest.mark.parametrize("mode", list(FillMode))
def test_every_mode_parses_from_its_own_value(mode: FillMode) -> None:
    assert parse_fill_mode(mode.value) is mode


@pytest.mark.parametrize(
    ("spelling", "mode"),
    [
        ("SIGNAL-BAR-CLOSE", FillMode.SIGNAL_BAR_CLOSE),
        ("Next-Session-Open", FillMode.NEXT_SESSION_OPEN),
        (" DECISION_MINUTE_OPEN ", FillMode.DECISION_MINUTE_OPEN),
        ("decision-minute-open", FillMode.DECISION_MINUTE_OPEN),
    ],
)
def test_case_space_and_hyphens_name_the_same_mode(spelling: str, mode: FillMode) -> None:
    assert parse_fill_mode(spelling) is mode


@pytest.mark.parametrize(
    ("short_name", "mode"),
    [
        ("close", FillMode.SIGNAL_BAR_CLOSE),
        ("SignalBarClose", FillMode.SIGNAL_BAR_CLOSE),
        ("open", FillMode.NEXT_BAR_OPEN),
        ("nextbaropen", FillMode.NEXT_BAR_OPEN),
    ],
)
def test_a_short_name_parses_only_on_a_surface_that_takes_the_aliases(short_name: str, mode: FillMode) -> None:
    assert parse_fill_mode(short_name, aliases=ENGINE_BACKTEST_ALIASES) is mode
    with pytest.raises(UnknownFillModeError):
        parse_fill_mode(short_name)


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
