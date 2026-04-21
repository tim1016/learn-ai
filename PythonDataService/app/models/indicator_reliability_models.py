"""Pydantic models for Indicator Reliability API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


StrengthLabel = Literal["Noise", "Weak", "Moderate", "Strong"]
StabilityLabel = Literal["Low", "Moderate", "High"]
DirectionLabel = Literal["Mean-Reversion", "Momentum", "None"]
TradeabilityLabel = Literal["Likely tradeable", "Marginal", "Unlikely", "Unknown"]


class VerdictModel(BaseModel):
    """Top-line verdict card summarizing the best-horizon analysis.

    ``tradeability`` is "Unknown" until Tranche 3 wires the IR proxy.
    """

    direction: DirectionLabel
    strength: StrengthLabel
    stability: StabilityLabel
    tradeability: TradeabilityLabel = "Unknown"
    horizon: int | None = Field(None, description="Horizon the verdict is computed on")


class IndicatorReliabilityRequest(BaseModel):
    """Request to analyze indicator reliability."""

    ticker: str = Field(..., description="Stock ticker symbol")
    indicator_name: str = Field(..., description="pandas-ta indicator name (e.g., 'rsi', 'ema')")
    indicator_params: dict[str, int | float] = Field(
        default_factory=dict,
        description="Indicator parameters (e.g., {'length': 14})",
    )
    start_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    end_date: str = Field(..., description="End date (YYYY-MM-DD)")
    horizons: list[int] = Field(
        default=[1, 5, 10, 15, 30],
        description="Forward horizons (in bars) to analyze",
    )
    include_slope: bool = Field(
        default=False,
        description="Also compute IC on indicator slope (1-bar change)",
    )
    timespan: str = Field(default="minute", description="Bar timespan")
    multiplier: int = Field(default=1, description="Bar multiplier")


class HorizonICResult(BaseModel):
    """IC analysis result for a single forward horizon with IS/OOS split."""

    horizon: int = Field(..., description="Forward horizon in bars")

    # In-sample statistics
    is_mean_ic: float = Field(..., description="In-sample mean IC")
    is_t_stat: float = Field(..., description="In-sample standard t-statistic")
    is_p_value: float = Field(..., description="In-sample standard p-value")
    is_nw_t_stat: float | None = Field(None, description="In-sample Newey-West t-stat")
    is_nw_p_value: float | None = Field(None, description="In-sample Newey-West p-value")
    is_effective_n: int = Field(..., description="In-sample effective sample size")

    # Out-of-sample statistics
    oos_mean_ic: float | None = Field(None, description="Out-of-sample mean IC")
    oos_t_stat: float | None = Field(None, description="Out-of-sample t-statistic")
    oos_p_value: float | None = Field(None, description="Out-of-sample p-value")
    oos_effective_n: int | None = Field(None, description="Out-of-sample effective N")
    oos_retention: float | None = Field(
        None, description="OOS IC / IS IC retention ratio"
    )

    # Multiple testing corrections (applied to IS p-value)
    bonferroni_p: float = Field(..., description="Bonferroni-corrected p-value")
    fdr_p: float = Field(..., description="FDR (Benjamini-Hochberg) corrected p-value")

    # Random baseline comparison
    random_baseline_mean: float = Field(
        0.0, description="Mean IC from random shuffled signals"
    )
    random_baseline_std: float = Field(
        0.0, description="Std of IC from random signals"
    )
    ic_vs_random_zscore: float = Field(
        0.0, description="Z-score: (is_mean_ic - random_mean) / random_std"
    )

    # Interpretations (legacy free-text)
    is_interpretation: str = Field(..., description="In-sample verdict")
    oos_interpretation: str | None = Field(None, description="Out-of-sample verdict")

    # Stability metrics
    is_hit_rate: float = Field(
        0.0, description="Fraction of daily ICs whose sign matches mean IC sign"
    )
    is_daily_ic_std: float = Field(0.0, description="Std dev of daily IC series")

    # Bucketed verdict labels
    strength_label: StrengthLabel = Field(
        "Noise", description="|IC| bucket: Noise/Weak/Moderate/Strong"
    )
    stability_label: StabilityLabel = Field(
        "Low", description="Hit-rate bucket: Low/Moderate/High"
    )
    direction_label: DirectionLabel = Field(
        "None", description="Signed IC: Mean-Reversion/Momentum/None"
    )

    # OOS delta as percentage (+24% = stronger, -40% = weaker). Nullable.
    retention_delta_pct: float | None = Field(
        None, description="(|OOS|/|IS| - 1) * 100; null if OOS absent"
    )

    # Slope decision flags (only populated for slope variant rows)
    slope_adds_value: bool | None = Field(
        None, description="True if slope IC is materially stronger than raw"
    )
    slope_recommended: bool | None = Field(
        None, description="True if slope adds value AND is OOS-validated"
    )


class DecayCurvePoint(BaseModel):
    """Single point on the IC-vs-horizon decay curve (IS period)."""

    horizon: int
    ic: float
    p_value: float
    ic_stderr: float = Field(
        0.0, description="Stderr of the IC estimate (NW-implied when available)"
    )


class RegimeICPoint(BaseModel):
    """IC for one horizon within one regime bucket."""

    horizon: int
    mean_ic: float
    t_stat: float
    p_value: float
    effective_n: int
    hit_rate: float
    bars_in_regime: int


class RegimeResults(BaseModel):
    """Regime-conditioned IC. Values are null when the bucket is too small."""

    high_vol: list[RegimeICPoint] | None = None
    low_vol: list[RegimeICPoint] | None = None
    vol_window: int = Field(
        20, description="Rolling window used to estimate realized vol (bars)"
    )


class IndicatorReliabilityResponse(BaseModel):
    """Response from indicator reliability analysis."""

    success: bool
    ticker: str
    indicator_name: str
    indicator_params: dict[str, int | float]
    display_name: str = Field(..., description="Formatted display name (e.g., 'RSI (14)')")
    category: str | None = Field(None, description="Indicator category (e.g., 'momentum')")
    start_date: str
    end_date: str
    bar_count: int = Field(..., description="Number of bars analyzed")

    # Train/test split info
    train_start: str | None = None
    train_end: str | None = None
    test_start: str | None = None
    test_end: str | None = None
    train_bars: int | None = None
    test_bars: int | None = None
    train_ratio: float = Field(default=0.7, description="Train split ratio used")

    # Results per horizon
    results: list[HorizonICResult] = Field(
        default_factory=list,
        description="IC results for each forward horizon",
    )

    # Optional slope IC results
    slope_results: list[HorizonICResult] | None = Field(
        None,
        description="IC results for indicator slope (if include_slope=True)",
    )

    # Daily IC series for charting (using best horizon, in-sample period)
    daily_ic_values: list[float] = Field(default_factory=list)
    daily_ic_dates: list[str] = Field(default_factory=list)
    best_horizon: int | None = Field(None, description="Horizon with strongest OOS signal")

    # Multiple testing summary
    any_significant_after_bonferroni: bool = Field(
        default=False,
        description="Any horizon significant after Bonferroni correction",
    )
    any_significant_after_fdr: bool = Field(
        default=False,
        description="Any horizon significant after FDR correction",
    )
    num_horizons_tested: int = Field(
        default=0, description="Number of horizons tested (for correction context)"
    )

    # Random baseline summary
    random_simulations: int = Field(default=100, description="Number of random shuffles")

    # Top-line verdict card (best-horizon summary)
    verdict: VerdictModel | None = Field(
        None, description="Top-line decision summary for the best horizon"
    )

    # Tranche 2 diagnostics
    decay_curve: list[DecayCurvePoint] = Field(
        default_factory=list,
        description="IC at every integer horizon 1..N on the in-sample period",
    )
    regime_results: RegimeResults | None = Field(
        None, description="IC conditioned on volatility regime (in-sample)"
    )

    # Warnings
    warnings: list[str] = Field(default_factory=list)

    error: str | None = None


class IndicatorInfo(BaseModel):
    """Metadata about an available indicator."""

    name: str
    category: str
    description: str
    params: list[dict] = Field(default_factory=list)
