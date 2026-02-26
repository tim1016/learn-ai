"""Fixed research parameters — locked for Phase 1."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchConfig:
    """Immutable research configuration.

    These parameters are locked for Phase 1 and must not be changed
    mid-study to maintain statistical rigour.
    """

    horizon: int = 15
    """Prediction horizon in bars (15-minute forward return)."""

    n_bins: int = 5
    """Number of quantile bins for monotonicity analysis."""

    min_series_length: int = 100
    """Minimum number of bars required to run validation."""

    adf_significance: float = 0.05
    """Significance level for ADF stationarity test."""

    kpss_significance: float = 0.05
    """Significance level for KPSS stationarity test."""

    ic_correlation_method: str = "spearman"
    """Correlation method for Information Coefficient (daily rank correlation)."""

    ic_significance: float = 0.10
    """p-value threshold for IC significance."""

    monotonicity_threshold: float = 0.75
    """Fraction of increasing quantile steps required for monotonicity pass."""
