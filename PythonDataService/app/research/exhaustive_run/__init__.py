"""Full-data and fixed-gap forward robustness derived from one walk-forward."""

from app.research.exhaustive_run.errors import (
    ExhaustiveRunAlreadyExistsError,
    ExhaustiveRunCorruptError,
    ExhaustiveRunNotFoundError,
    ExhaustiveRunSourceError,
)
from app.research.exhaustive_run.result import (
    CandidateFoldEvidence,
    CandidateForwardStability,
    ExhaustiveCandidateResult,
    ExhaustiveRunConfig,
    ExhaustiveRunResult,
    FoldCandidateSelection,
    TradeRecencySummary,
)
from app.research.exhaustive_run.runner import run_exhaustive_analysis
from app.research.exhaustive_run.storage import (
    load_exhaustive_run,
    load_latest_for_walk_forward,
    save_exhaustive_run,
)

__all__ = [
    "CandidateFoldEvidence",
    "CandidateForwardStability",
    "ExhaustiveCandidateResult",
    "ExhaustiveRunAlreadyExistsError",
    "ExhaustiveRunConfig",
    "ExhaustiveRunCorruptError",
    "ExhaustiveRunNotFoundError",
    "ExhaustiveRunResult",
    "ExhaustiveRunSourceError",
    "FoldCandidateSelection",
    "TradeRecencySummary",
    "load_exhaustive_run",
    "load_latest_for_walk_forward",
    "run_exhaustive_analysis",
    "save_exhaustive_run",
]
