"""Training-only selection tests for the Exhaustive Run leaderboard."""

from __future__ import annotations

import pytest

from app.research.exhaustive_run.selection import (
    select_fold_candidates,
    selected_candidate_configs,
)
from app.research.runs.result import RunMetrics
from app.research.walk_forward.result import (
    FoldResult,
    ParameterCandidateConfig,
    ParameterSearchConfig,
    TrainingCandidateResult,
    WalkForwardConfig,
    WalkForwardResult,
)


def _metrics(*, sharpe: float, total_return: float, trades: int) -> RunMetrics:
    return RunMetrics(
        total_trades=trades,
        winning_trades=trades,
        losing_trades=0,
        win_rate=1.0,
        total_return_pct=total_return,
        max_drawdown_pct=0.0,
        sharpe_ratio=sharpe,
    )


def _candidate(gap: float, *, sharpe: float, total_return: float, eligible: bool = True) -> TrainingCandidateResult:
    return TrainingCandidateResult(
        parameters={"gap_bps": gap},
        train_run_id=f"train-{gap}",
        train_metrics=_metrics(sharpe=sharpe, total_return=total_return, trades=10),
        eligible=eligible,
    )


def _source() -> tuple[WalkForwardConfig, WalkForwardResult]:
    candidates = [
        ParameterCandidateConfig(
            parameters={"gap_bps": gap},
            strategy_spec_hash=f"hash-{gap}",
            strategy_spec_json={},
        )
        for gap in (1.0, 2.0, 3.0, 4.0)
    ]
    config = WalkForwardConfig(
        walk_forward_id="a" * 32,
        strategy_spec_hash="source",
        strategy_spec_json={},
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1,
        end_ms=10,
        initial_cash=100_000.0,
        fill_mode="next_bar_open",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=0,
        split_policy={"kind": "rolling", "train_days": 180, "test_days": 30, "step_days": 30},
        parameter_search=ParameterSearchConfig(candidates=candidates, min_trades=5),
        created_at_ms=1,
    )
    fold = FoldResult(
        fold_index=0,
        train_start_ms=1,
        train_end_ms=2,
        test_start_ms=2,
        test_end_ms=3,
        test_run_id="test",
        test_metrics=_metrics(sharpe=1.0, total_return=0.01, trades=1),
        test_trade_count=1,
        selected_parameters={"gap_bps": 1.0},
        training_candidates=[
            _candidate(1.0, sharpe=3.0, total_return=0.0),
            _candidate(2.0, sharpe=2.0, total_return=1.0),
            _candidate(3.0, sharpe=1.0, total_return=0.5),
            _candidate(4.0, sharpe=99.0, total_return=99.0, eligible=False),
        ],
    )
    result = WalkForwardResult(
        walk_forward_id=config.walk_forward_id,
        strategy_spec_hash="source",
        split_policy=config.split_policy,
        folds=[fold],
        created_at_ms=1,
        completed_at_ms=2,
    )
    return config, result


def test_selection_balances_train_sharpe_and_return_inside_each_fold() -> None:
    config, result = _source()

    selections = select_fold_candidates(config, result, max_per_fold=2)

    assert [selection.parameters["gap_bps"] for selection in selections] == [2.0, 1.0]
    assert selections[0].selection_score == pytest.approx(0.75, abs=1e-12, rel=0)
    assert selections[1].selection_score == pytest.approx(0.5, abs=1e-12, rel=0)
    assert all(selection.parameters["gap_bps"] != 4.0 for selection in selections)


def test_selected_configs_deduplicate_repeated_fold_appearances() -> None:
    config, result = _source()
    selections = select_fold_candidates(config, result, max_per_fold=3)
    repeated = [*selections, selections[0].model_copy(update={"fold_index": 1})]

    unique = selected_candidate_configs(config, repeated)

    assert [candidate.parameters["gap_bps"] for candidate in unique] == [1.0, 2.0, 3.0]
