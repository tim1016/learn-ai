"""Training-only leaderboard selection for the Exhaustive Run protocol.

Formula: score = 0.5 * percentile_rank(train_sharpe)
  + 0.5 * percentile_rank(train_total_return), calculated among eligible
  candidates inside the same fold.
Reference: repository-internal protocol contract documented in
  docs/references/spy-ema-exhaustive-run.md.
Canonical implementation: this file.
Validated against: tests/research/exhaustive_run/test_selection.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.research.exhaustive_run.result import FoldCandidateSelection
from app.research.walk_forward.result import (
    ParameterCandidateConfig,
    TrainingCandidateResult,
    WalkForwardConfig,
    WalkForwardResult,
)


def select_fold_candidates(
    config: WalkForwardConfig,
    result: WalkForwardResult,
    *,
    max_per_fold: int = 5,
) -> list[FoldCandidateSelection]:
    """Return up to ``max_per_fold`` training-only candidates per fold."""
    if max_per_fold < 1:
        raise ValueError("max_per_fold must be positive")
    if config.parameter_search is None:
        raise ValueError("source walk-forward has no persisted parameter search")

    declared = {
        _parameter_key(candidate.parameters): (index, candidate)
        for index, candidate in enumerate(config.parameter_search.candidates)
    }
    selections: list[FoldCandidateSelection] = []
    for fold in result.folds:
        eligible = [
            candidate
            for candidate in fold.training_candidates
            if candidate.eligible and candidate.train_metrics.sharpe_ratio is not None
        ]
        if not eligible:
            continue

        sharpes = [candidate.train_metrics.sharpe_ratio for candidate in eligible]
        returns = [candidate.train_metrics.total_return_pct for candidate in eligible]
        scored: list[
            tuple[float, int, TrainingCandidateResult, ParameterCandidateConfig]
        ] = []
        for candidate in eligible:
            declared_entry = declared.get(_parameter_key(candidate.parameters))
            if declared_entry is None:
                raise ValueError(
                    f"fold {fold.fold_index} candidate has no matching persisted specification"
                )
            declaration_index, candidate_config = declared_entry
            score = 0.5 * _percentile_rank(candidate.train_metrics.sharpe_ratio, sharpes)
            score += 0.5 * _percentile_rank(candidate.train_metrics.total_return_pct, returns)
            scored.append((score, declaration_index, candidate, candidate_config))

        scored.sort(
            key=lambda entry: (
                entry[0],
                entry[2].train_metrics.sharpe_ratio,
                entry[2].train_metrics.total_return_pct,
                entry[2].train_metrics.total_trades,
                -entry[1],
            ),
            reverse=True,
        )
        for rank, (score, _declaration_index, candidate, candidate_config) in enumerate(
            scored[:max_per_fold],
            start=1,
        ):
            selections.append(
                FoldCandidateSelection(
                    fold_index=fold.fold_index,
                    fold_rank=rank,
                    parameters=dict(candidate.parameters),
                    strategy_spec_hash=candidate_config.strategy_spec_hash,
                    train_run_id=candidate.train_run_id,
                    train_metrics=candidate.train_metrics,
                    selection_score=score,
                )
            )
    return selections


def selected_candidate_configs(
    config: WalkForwardConfig,
    selections: Sequence[FoldCandidateSelection],
) -> list[ParameterCandidateConfig]:
    """Return selected unique specifications in declaration order."""
    if config.parameter_search is None:
        return []
    selected_hashes = {selection.strategy_spec_hash for selection in selections}
    return [
        candidate
        for candidate in config.parameter_search.candidates
        if candidate.strategy_spec_hash in selected_hashes
    ]


def _parameter_key(parameters: Mapping[str, float]) -> tuple[tuple[str, float], ...]:
    return tuple(sorted(parameters.items()))


def _percentile_rank(value: float, values: Sequence[float]) -> float:
    """Return a tie-aware empirical percentile in ``[0, 1]``."""
    if len(values) == 1:
        return 1.0
    lower = sum(candidate < value for candidate in values)
    equal_others = sum(candidate == value for candidate in values) - 1
    return (lower + 0.5 * equal_others) / (len(values) - 1)
