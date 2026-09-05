"""Fold generation (PRD #1925 "Testing decisions — Splits")."""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.walk_forward_study.folds import FoldPlanError, add_months, plan_folds, whole_months_between


def test_a_clean_two_year_range_with_six_and_three_months_gives_six_contiguous_folds() -> None:
    folds = plan_folds(start=date(2024, 1, 1), end_exclusive=date(2026, 1, 1), training_months=6, test_months=3)

    assert [f.fold_index for f in folds] == list(range(6))
    assert folds[0].train_start == date(2024, 1, 2)  # Jan 1 is a holiday → snapped forward
    assert folds[0].test_start == folds[0].train_end == date(2024, 7, 1)
    assert folds[-1].test_end == date(2026, 1, 2)  # the study end, snapped like every other boundary
    # Every month after the training window is scored exactly once: test windows tile with no gap or overlap.
    for previous, following in pairwise(folds):
        assert following.test_start == previous.test_end
    covered = set()
    for fold in folds:
        sessions = set(expected_sessions(fold.test_start, fold.test_end)) - {fold.test_end}
        assert not (sessions & covered)
        covered |= sessions
    assert covered == set(expected_sessions(date(2024, 7, 1), date(2026, 1, 1)))


def test_month_addition_is_start_anchored_and_clamps_to_month_end() -> None:
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    # Start-anchored: Jan 31 + 2 months is Mar 31, not Feb 29 + 1 month = Mar 29.
    assert add_months(date(2024, 1, 31), 2) == date(2024, 3, 31)
    assert add_months(add_months(date(2024, 1, 31), 1), 1) == date(2024, 3, 29)
    assert whole_months_between(date(2024, 1, 31), date(2024, 3, 31)) == 2
    assert whole_months_between(date(2024, 1, 31), date(2024, 3, 29)) is None


def test_weekend_and_holiday_cuts_snap_to_the_next_session_identically_on_both_sides() -> None:
    # 2025-03-01 is a Saturday; 2025-06-01 is a Sunday.
    folds = plan_folds(start=date(2024, 12, 1), end_exclusive=date(2025, 6, 1), training_months=3, test_months=3)

    assert folds[0].train_start == date(2024, 12, 2)
    assert folds[0].train_end == folds[0].test_start == date(2025, 3, 3)
    assert folds[0].test_end == date(2025, 6, 2)


def test_a_trailing_tail_is_refused_and_the_nearest_valid_ends_are_named() -> None:
    with pytest.raises(FoldPlanError) as excinfo:
        plan_folds(start=date(2024, 1, 1), end_exclusive=date(2025, 12, 1), training_months=6, test_months=3)  # 23 months

    assert excinfo.value.nearest_valid_ends == (date(2025, 10, 1), date(2026, 1, 1))
    assert "2025-10-01" in str(excinfo.value) and "2026-01-01" in str(excinfo.value)


def test_a_range_that_is_not_whole_months_is_refused_naming_the_neighbours() -> None:
    with pytest.raises(FoldPlanError) as excinfo:
        plan_folds(start=date(2024, 1, 1), end_exclusive=date(2025, 12, 15), training_months=6, test_months=3)

    assert excinfo.value.nearest_valid_ends == (date(2025, 12, 1), date(2026, 1, 1))


def test_training_plus_test_exceeding_the_range_is_refused_before_execution() -> None:
    with pytest.raises(FoldPlanError, match="no room for a single fold"):
        plan_folds(start=date(2024, 1, 1), end_exclusive=date(2024, 7, 1), training_months=6, test_months=3)


def test_an_exactly_dividing_range_admits_and_its_final_test_window_ends_on_the_study_end() -> None:
    folds = plan_folds(start=date(2024, 1, 1), end_exclusive=date(2024, 10, 1), training_months=6, test_months=3)

    assert len(folds) == 1
    assert folds[0].test_end == date(2024, 10, 1)


def test_dst_transitions_do_not_matter_to_calendar_dates() -> None:
    folds = plan_folds(start=date(2024, 2, 1), end_exclusive=date(2024, 12, 1), training_months=4, test_months=3)

    assert [f.test_start for f in folds] == [date(2024, 6, 3), date(2024, 9, 3)]  # Jun 1 Sat → Mon; Sep 1 Sun, Sep 2 Labor Day → Tue
