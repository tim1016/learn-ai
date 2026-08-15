"""Deterministic train-side parameter selection for walk-forward folds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.engine.strategy.spec import StrategySpec
from app.research.walk_forward.result import TrainingCandidateResult


class ParameterSelectionError(RuntimeError):
    """Raised when a fold has no candidate with sufficient train evidence."""

    def __init__(
        self,
        message: str,
        *,
        candidates: tuple[TrainingCandidateResult, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.candidates = candidates
        self.warnings = warnings


@dataclass(frozen=True)
class ParameterCandidate:
    """A named parameter assignment plus the exact spec it materializes."""

    parameters: Mapping[str, float]
    spec: StrategySpec

    def __post_init__(self) -> None:
        if not self.parameters:
            raise ValueError("parameter candidate requires a non-empty assignment")
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )


@dataclass(frozen=True)
class ParameterSearchRequest:
    """Internal request for deterministic train-window selection."""

    candidates: tuple[ParameterCandidate, ...]
    min_trades: int = 5

    def __post_init__(self) -> None:
        if len(self.candidates) < 2:
            raise ValueError("parameter search requires at least two candidates")
        if self.min_trades < 1:
            raise ValueError("parameter search min_trades must be at least 1")
        assignments = [tuple(sorted(candidate.parameters.items())) for candidate in self.candidates]
        if len(assignments) != len(set(assignments)):
            raise ValueError("parameter candidate assignments must be unique")


def select_candidate_index(results: list[TrainingCandidateResult]) -> int:
    """Return the deterministic winner's index in declaration order.

    Formula: argmax eligible candidate by
      (train Sharpe, train total return, -declaration index).
    Reference: docs/references/spy-ema-normalized-gap-walk-forward.md.
    Canonical implementation: this file.
    Validated against: tests/research/walk_forward/test_selection.py.

    ``eligible`` is assigned by the runner after engine completion and the
    configured minimum-trade gate. A non-null train Sharpe is mandatory;
    the protocol fails closed rather than silently changing objectives.
    """
    ranked = [
        (index, result)
        for index, result in enumerate(results)
        if result.eligible and result.train_metrics.sharpe_ratio is not None
    ]
    if not ranked:
        raise ParameterSelectionError("no parameter candidate produced an eligible non-null train Sharpe")
    winner_index, _ = max(
        ranked,
        key=lambda pair: (
            pair[1].train_metrics.sharpe_ratio,
            pair[1].train_metrics.total_return_pct,
            -pair[0],
        ),
    )
    return winner_index
