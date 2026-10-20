"""Deterministic selection-policy tests for Phase 4B walk-forward."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.research.runs.result import RunMetrics
from app.research.walk_forward.result import TrainingCandidateResult
from app.research.walk_forward.selection import (
    ParameterSelectionError,
    select_candidate_index,
)


def _candidate(
    threshold: float,
    *,
    sharpe: float | None,
    total_return: float,
    eligible: bool = True,
) -> TrainingCandidateResult:
    return TrainingCandidateResult(
        parameters={"gap_bps": threshold},
        train_run_id=str(int(threshold * 10)).zfill(32),
        train_metrics=RunMetrics(
            total_trades=8,
            winning_trades=5,
            losing_trades=3,
            win_rate=0.625,
            total_return_pct=total_return,
            sharpe_ratio=sharpe,
        ),
        eligible=eligible,
        ineligibility_reason=None if eligible else "below minimum trade count",
    )


@pytest.mark.parametrize(
    ("make_candidates", "expected_index"),
    [
        pytest.param(
            lambda: [
                _candidate(1, sharpe=0.8, total_return=0.03),
                _candidate(2, sharpe=1.1, total_return=0.01),
                _candidate(3, sharpe=1.1, total_return=0.02),
            ],
            2,
            id="prioritizes_sharpe_then_return",
        ),
        pytest.param(
            lambda: [
                _candidate(1, sharpe=1.1, total_return=0.02),
                _candidate(2, sharpe=1.1, total_return=0.02),
            ],
            0,
            id="uses_declaration_order_as_final_tie_break",
        ),
        pytest.param(
            lambda: [
                _candidate(1, sharpe=2.0, total_return=0.03, eligible=False),
                _candidate(2, sharpe=None, total_return=0.05),
                _candidate(3, sharpe=0.4, total_return=0.01),
            ],
            2,
            id="excludes_ineligible_or_null_sharpe",
        ),
    ],
)
def test_select_candidate_index_picks_the_winner(
    make_candidates: Callable[[], list[TrainingCandidateResult]], expected_index: int
) -> None:
    assert select_candidate_index(make_candidates()) == expected_index


def test_select_candidate_fails_closed_without_eligible_sharpe() -> None:
    with pytest.raises(ParameterSelectionError, match="eligible non-null train Sharpe"):
        select_candidate_index(
            [
                _candidate(1, sharpe=None, total_return=0.05),
                _candidate(2, sharpe=1.0, total_return=0.01, eligible=False),
            ]
        )
