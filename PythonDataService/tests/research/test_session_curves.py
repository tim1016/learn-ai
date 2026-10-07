"""Runs' curves against one list of session closes (hand-computed values, atol=1e-12, rtol=0)."""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.research.session_curves import curves_read, session_curves
from app.research.sweep.daily_equity import drawdown_series


def test_a_curve_is_the_return_on_starting_capital_and_the_fall_from_its_own_peak() -> None:
    days = [session_close_ms_utc(day) for day in (date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8))]
    daily = [(days[0], 101_000.0), (days[1], 99_990.0), (days[2], 102_010.0)]

    sessions, curves = session_curves({"k": daily}, 100_000.0)

    assert sessions == days and curves["k"]["first_session"] == 0
    assert curves["k"]["cumulative_return"] == pytest.approx([0.01, -0.0001, 0.0201], abs=1e-12, rel=0)
    assert curves["k"]["drawdown"] == pytest.approx([0.0, 99_990.0 / 101_000.0 - 1.0, 0.0], abs=1e-12, rel=0)
    assert curves["k"]["drawdown"] == [value for _, value in drawdown_series(daily)]
    # The deepest fall is the series' own lowest point, worked out here and not in the browser.
    assert curves["k"]["deepest_drawdown"] == pytest.approx(99_990.0 / 101_000.0 - 1.0, abs=1e-12, rel=0)


def test_a_run_whose_equity_never_rose_above_zero_has_a_return_and_no_fall_from_peak() -> None:
    _, curves = session_curves({"k": [(1, 0.0), (2, 0.0)]}, 100_000.0)
    assert curves["k"]["cumulative_return"] == [-1.0, -1.0] and curves["k"]["drawdown"] is None and curves["k"]["deepest_drawdown"] is None


def test_a_read_sends_its_sessions_once_and_each_curve_starts_at_its_own_first_session() -> None:
    # Two folds' test runs one after the other, and a run over both that missed the session at 3.
    stored = {"early": [(1, 110.0), (2, 121.0)], "late": [(3, 90.0), (4, 99.0)], "gapped": [(1, 100.0), (2, 105.0), (4, 84.0)]}

    sessions, curves = session_curves(stored, 100.0)

    assert sessions == [1, 2, 3, 4]
    assert (curves["early"]["first_session"], curves["early"]["cumulative_return"]) == (0, pytest.approx([0.1, 0.21], abs=1e-12, rel=0))
    assert (curves["late"]["first_session"], curves["late"]["cumulative_return"]) == (2, pytest.approx([-0.1, -0.01], abs=1e-12, rel=0))
    # A session inside a run's span that it did not score is a gap, never a carried value.
    assert curves["gapped"]["first_session"] == 0 and curves["gapped"]["cumulative_return"][2] is None and curves["gapped"]["drawdown"][2] is None
    assert curves["gapped"]["drawdown"][3] == pytest.approx(84.0 / 105.0 - 1.0, abs=1e-12, rel=0) == curves["gapped"]["deepest_drawdown"]


def test_a_session_the_caller_names_stands_on_the_axis_though_no_run_scored_it() -> None:
    # A walk-forward fold that did not run: its sessions (3 and 4) are on the axis, empty, between two folds that did.
    sessions, curves = session_curves({"first": [(1, 110.0), (2, 121.0)], "third": [(5, 90.0)]}, 100.0, also=[1, 2, 3, 4, 5, 6])

    assert sessions == [1, 2, 3, 4, 5, 6]
    assert (curves["first"]["first_session"], curves["first"]["cumulative_return"]) == (0, pytest.approx([0.1, 0.21], abs=1e-12, rel=0))
    assert (curves["third"]["first_session"], curves["third"]["cumulative_return"]) == (4, pytest.approx([-0.1], abs=1e-12, rel=0))


def test_curves_come_back_in_the_order_asked_and_a_run_that_kept_none_is_named_not_drawn() -> None:
    stored = {"b": [(1, 100.0)], "a": [(1, 110.0)], "unasked": [(9, 100.0)]}

    read = curves_read(["a", "old", "b"], stored, 100.0, key="params_hash")

    assert [curve["params_hash"] for curve in read["curves"]] == ["a", "b"]
    assert read["not_recorded"] == ["old"]
    # The sessions are those of the runs asked for, not of every run that kept a curve.
    assert read["sessions_ms"] == [1]


def test_a_read_names_its_curves_under_any_key_and_keeps_the_sessions_the_caller_names() -> None:
    # Walk-forward folds, by index: fold 0 drawn, fold 1 completed with no curve kept, and a session (3) no fold scored.
    read = curves_read([0, 1], {0: [(1, 110.0), (2, 121.0)]}, 100.0, key="fold_index", also=[1, 2, 3])

    assert read["sessions_ms"] == [1, 2, 3] and read["not_recorded"] == [1]
    (curve,) = read["curves"]
    assert (curve["fold_index"], curve["first_session"]) == (0, 0)
    assert curve["cumulative_return"] == pytest.approx([0.1, 0.21], abs=1e-12, rel=0)
