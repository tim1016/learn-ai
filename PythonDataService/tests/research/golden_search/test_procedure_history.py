"""Chronological validation math: fold windows, linked fold returns, fold evidence."""

from __future__ import annotations

import math
from datetime import date
from itertools import pairwise

import numpy as np

from app.research.golden_search.procedure_history import fold_evidence, fold_return, fold_windows, link_fold_returns
from app.research.walk_forward_study.verdict import compute_verdict
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search import declaration, metrics, protocol


def test_link_fold_returns_compounds_growth_of_one_across_folds() -> None:
    linked = link_fold_returns([0.10, -0.05, 0.02])

    # 1.1 - 1 = 0.1; 1.1 x 0.95 - 1 = 0.045; 1.045 x 1.02 - 1 = 0.0659.
    assert np.allclose(linked, [0.10, 0.045, 0.0659], atol=1e-9, rtol=0)


def test_link_fold_returns_a_missing_fold_breaks_the_line_and_never_interpolates() -> None:
    broken = link_fold_returns([0.10, None, 0.20])
    assert np.isclose(broken[0], 0.10, atol=1e-9, rtol=0)
    assert broken[1:] == [None, None]
    assert link_fold_returns([None, 0.10]) == [None, None]
    assert link_fold_returns([0.10, math.nan, 0.20])[1:] == [None, None]
    assert link_fold_returns([]) == []


def test_fold_return_is_the_completed_test_runs_return_on_fresh_capital() -> None:
    assert fold_return(metrics(1.0, total_return=0.034)) == 0.034
    assert fold_return(metrics(1.0, status="failed")) is None
    assert fold_return(metrics(1.0, total_return=None)) is None
    assert fold_return(None) is None


def test_fold_evidence_feeds_the_legacy_verdict_and_records_missing_winners_as_failed() -> None:
    folds = [
        fold_evidence(0, metrics(2.0), metrics(1.5, trades=20)),
        fold_evidence(1, metrics(1.0), metrics(0.8, trades=15)),
    ]

    verdict = compute_verdict(folds, min_trades=30)

    assert folds[0].retention == 0.75
    assert verdict.label == "still worked"
    assert verdict.based_on == "based on 2 of 2 folds"
    no_winner = fold_evidence(2, None, None)
    assert no_winner.status == "failed"
    assert compute_verdict([*folds, no_winner], min_trades=30).label == "could not be judged"
    assert fold_evidence(3, metrics(1.0), metrics(1.0, status="failed")).status == "failed"


def test_fold_windows_tile_the_development_interval_and_never_leave_it() -> None:
    plan = protocol(declaration())

    windows = fold_windows(plan)

    assert [w.fold_index for w in windows] == [0, 1, 2]
    # Jan 1 is a holiday: the planner snaps the first training start to Jan 2.
    assert windows[0].train_start_ms == et_midnight_ms(date(2024, 1, 2))
    for previous, following in pairwise(windows):
        assert following.test_start_ms == previous.test_end_ms
    # The planner snaps the last end to the next session (2025-01-02); it is clipped back to
    # the development end, which drops no session.
    assert windows[-1].test_end_ms == plan.development_end_ms == et_midnight_ms(date(2025, 1, 1))
    assert all(w.train_end_ms == w.test_start_ms <= plan.development_end_ms for w in windows)
