"""Analysis submodule: per-bar diff stats, trade-level matching, etc."""

from app.research.divergence.analysis.bar_divergence import (
    DivergenceMatrix,
    IndicatorPair,
    diff_stats,
    pairwise_diff,
    run_full_comparison,
)

__all__ = [
    "DivergenceMatrix",
    "IndicatorPair",
    "diff_stats",
    "pairwise_diff",
    "run_full_comparison",
]
