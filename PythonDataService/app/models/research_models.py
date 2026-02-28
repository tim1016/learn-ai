"""Pydantic v2 models for research endpoint request/response."""
from __future__ import annotations

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    """Single OHLCV bar."""

    timestamp: int = Field(..., description="Milliseconds since epoch")
    open: float
    high: float
    low: float
    close: float
    volume: float


class RunFeatureResearchRequest(BaseModel):
    """Request body for POST /research/run-feature."""

    ticker: str = Field(..., description="Stock symbol (e.g. AAPL)")
    feature_name: str = Field(..., description="Feature to validate (e.g. momentum_5m)")
    bars: list[OHLCVBar] = Field(..., description="OHLCV bars (1-minute)")
    start_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    end_date: str = Field(..., description="ISO date (YYYY-MM-DD)")


class QuantileBinResponse(BaseModel):
    """Single quantile bin in the response."""

    bin_number: int
    lower_bound: float
    upper_bound: float
    mean_return: float
    count: int


class MonthlyICBreakdownResponse(BaseModel):
    """Monthly IC statistics."""

    month: str
    mean_ic: float
    t_stat: float
    observation_count: int


class RollingTStatPointResponse(BaseModel):
    """Single point in rolling smoothed t-stat series."""

    month: str
    t_stat_smoothed: float


class RegimeICResponse(BaseModel):
    """IC computed within a specific market regime."""

    regime_label: str
    mean_ic: float
    t_stat: float
    observation_count: int


class TrainTestSplitResponse(BaseModel):
    """Chronological train/test split IC comparison."""

    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_mean_ic: float
    train_t_stat: float
    train_days: int
    test_mean_ic: float
    test_t_stat: float
    test_days: int
    overfit_flag: bool
    oos_retention: float = 0.0
    oos_retention_label: str = "Unknown"


class StructuralBreakPointResponse(BaseModel):
    """A detected structural break in the IC series."""

    date: str
    ic_before: float
    ic_after: float
    t_stat: float
    significant: bool


class RobustnessResponse(BaseModel):
    """Complete robustness analysis."""

    monthly_breakdown: list[MonthlyICBreakdownResponse] = []
    pct_positive_months: float = 0.0
    pct_significant_months: float = 0.0
    best_month_ic: float = 0.0
    worst_month_ic: float = 0.0
    stability_label: str = "Unknown"
    pct_sign_consistent_months: float = 0.0
    sign_consistent_stability_label: str = "Unknown"
    rolling_t_stat: list[RollingTStatPointResponse] = []
    volatility_regimes: list[RegimeICResponse] = []
    trend_regimes: list[RegimeICResponse] = []
    train_test: TrainTestSplitResponse | None = None
    structural_breaks: list[StructuralBreakPointResponse] = []


class RunFeatureResearchResponse(BaseModel):
    """Response body for POST /research/run-feature."""

    success: bool
    ticker: str
    feature_name: str
    start_date: str
    end_date: str
    bars_used: int
    mean_ic: float
    ic_t_stat: float
    ic_p_value: float
    nw_t_stat: float = 0.0
    nw_p_value: float = 1.0
    effective_n: float = 0.0
    adf_pvalue: float
    kpss_pvalue: float
    is_stationary: bool
    passed_validation: bool
    quantile_bins: list[QuantileBinResponse] = []
    is_monotonic: bool = False
    monotonicity_ratio: float = 0.0
    ic_values: list[float] = []
    ic_dates: list[str] = []
    robustness: RobustnessResponse | None = None
    error: str | None = None


class FeatureInfoResponse(BaseModel):
    """Feature metadata for the information panel."""

    name: str
    formula_latex: str
    variables: str
    example: str
    interpretation: str
    implementation_note: str
    window: int
    category: str


# ─── Signal Engine Models ─────────────────────────────────────


class RunSignalEngineRequest(BaseModel):
    """Request body for POST /research/run-signal."""

    ticker: str = Field(..., description="Stock symbol (e.g. AAPL)")
    feature_name: str = Field(default="momentum_5m", description="Feature to test")
    bars: list[OHLCVBar] = Field(..., description="OHLCV bars (1-minute)")
    start_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    end_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    flip_sign: bool = Field(default=True, description="Flip sign for negative IC features")
    regime_gate_enabled: bool = Field(default=True, description="Enable regime gating")


class BacktestResultResponse(BaseModel):
    """Single backtest result for a threshold x cost config."""

    threshold: float
    cost_bps: float
    dates: list[str] = []
    cumulative_returns: list[float] = []
    positions: list[float] = []
    gross_sharpe: float = 0.0
    net_sharpe: float = 0.0
    max_drawdown: float = 0.0
    annualized_turnover: float = 0.0
    avg_holding_bars: float = 0.0
    win_rate: float = 0.0
    avg_win_loss_ratio: float = 0.0
    total_trades: int = 0
    net_total_return: float = 0.0
    gross_total_return: float = 0.0


class WalkForwardWindowResponse(BaseModel):
    """Single walk-forward fold result."""

    fold_index: int = 0
    train_start: str = ""
    train_end: str = ""
    test_start: str = ""
    test_end: str = ""
    train_bars: int = 0
    test_bars: int = 0
    mu: float = 0.0
    sigma: float = 0.0
    best_threshold: float = 0.0
    oos_net_sharpe: float = 0.0
    oos_gross_sharpe: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_net_return: float = 0.0
    oos_win_rate: float = 0.0
    oos_total_trades: int = 0
    oos_dates: list[str] = []
    oos_cumulative_returns: list[float] = []


class WalkForwardResultResponse(BaseModel):
    """Aggregated walk-forward validation results."""

    windows: list[WalkForwardWindowResponse] = []
    mean_oos_sharpe: float = 0.0
    std_oos_sharpe: float = 0.0
    median_oos_sharpe: float = 0.0
    pct_windows_profitable: float = 0.0
    pct_windows_positive_sharpe: float = 0.0
    worst_window_sharpe: float = 0.0
    best_window_sharpe: float = 0.0
    total_oos_bars: int = 0
    combined_oos_dates: list[str] = []
    combined_oos_cumulative_returns: list[float] = []
    oos_sharpe_trend_slope: float = 0.0


class GraduationCriterionResponse(BaseModel):
    """Single graduation criterion."""

    name: str = ""
    description: str = ""
    passed: bool = False
    value: float = 0.0
    threshold: float = 0.0
    label: str = "Fail"
    failure_reason: str = ""


class ParameterStabilityResponse(BaseModel):
    """Parameter stability assessment."""

    sharpe_values_by_threshold: dict[float, float] = {}
    stability_score: float = 0.0
    stability_label: str = "Fragile"


class GraduationResultResponse(BaseModel):
    """Complete graduation assessment."""

    criteria: list[GraduationCriterionResponse] = []
    overall_passed: bool = False
    overall_grade: str = "F"
    summary: str = ""
    status_label: str = "Exploratory"
    parameter_stability: ParameterStabilityResponse | None = None


class SignalDiagnosticsResponse(BaseModel):
    """Signal diagnostics."""

    signal_mean: float = 0.0
    signal_std: float = 0.0
    pct_time_active: float = 0.0
    avg_abs_signal: float = 0.0
    pct_filtered_by_threshold: float = 0.0
    pct_gated_by_regime: float = 0.0


class DataSufficiencyResponse(BaseModel):
    """Data sufficiency assessment."""

    total_bars: int = 0
    train_bars: int = 0
    test_bars: int = 0
    walk_forward_folds: int = 0
    effective_oos_bars: int = 0
    regimes_covered: int = 0
    regime_coverage: dict[str, int] = {}
    coverage_warnings: list[str] = []


class EffectiveSampleSizeResponse(BaseModel):
    """Autocorrelation-adjusted sample size."""

    raw_n: int = 0
    effective_n: float = 0.0
    autocorrelation_lag1: float = 0.0
    independent_bets: int = 0


class RunSignalEngineResponse(BaseModel):
    """Response body for POST /research/run-signal."""

    success: bool
    ticker: str
    feature_name: str
    start_date: str
    end_date: str
    bars_used: int = 0
    flip_sign: bool = True
    thresholds_tested: list[float] = []
    cost_bps_options: list[float] = []
    best_threshold: float = 0.0
    best_cost_bps: float = 0.0
    backtest_grid: list[BacktestResultResponse] = []
    walk_forward: WalkForwardResultResponse | None = None
    graduation: GraduationResultResponse | None = None
    signal_diagnostics: SignalDiagnosticsResponse | None = None
    data_sufficiency: DataSufficiencyResponse | None = None
    effective_sample: EffectiveSampleSizeResponse | None = None
    regime_coverage: dict[str, int] = {}
    research_log: str = ""
    error: str | None = None
