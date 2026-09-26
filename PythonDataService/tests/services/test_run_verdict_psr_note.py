"""The verdict's Probabilistic Sharpe note states its selection limit (#2462).

PSR is computed from one run's return series (``statistics.py``); a
Strategy Lab operator typically tries several settings and reads the best
run's PSR, and the best of N pure-noise candidates clears PSR ≥ 0.95 most
of the time. The frozen v2 policy's scores and thresholds stay untouched;
only the explanation changes: the ≥ 0.95 buckets must not read as
"Near-certain" or "High statistical confidence", because the figure is not
adjusted for how many settings were tried.
"""

from __future__ import annotations

import pytest

from app.services.run_verdict_service import _grade_psr_sub

_BANNED = ("Near-certain", "High statistical confidence")
_SELECTION_MARKER = "not adjusted for picking the best of several tried settings"


@pytest.mark.parametrize(("v", "expected_score"), [(0.97, 20), (0.999, 18)])
def test_a_high_psr_note_names_the_missing_selection_adjustment(v: float, expected_score: int) -> None:
    sub = _grade_psr_sub(v)

    assert sub.score == expected_score, "the frozen v2 score buckets must not move"
    assert _SELECTION_MARKER in sub.note
    assert not any(phrase in (sub.note or "") for phrase in _BANNED)


@pytest.mark.parametrize(
    ("v", "expected_score"),
    [(0.0, 2), (0.5, 8), (0.8, 14), (0.95, 20), (0.99, 18), (None, None)],
)
def test_the_frozen_v2_thresholds_and_scores_are_unchanged(v: float | None, expected_score: int | None) -> None:
    assert _grade_psr_sub(v).score == expected_score
