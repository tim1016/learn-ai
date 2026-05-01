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


class FeatureValidationSpecResponse(BaseModel):
    """Per-feature validation contract surfaced to the UI.

    Documents the question the screens are answering for this feature
    so the reader can spot a "wrong target" or "wrong shape" mismatch
    before reading the verdict.
    """

    feature_name: str = ""
    default_target: str = ""
    expected_direction: str = "unknown"
    expected_shape: str = "none"
    stationarity_required: bool = False
    monotonicity_required: bool = False
    is_signed_target_appropriate: bool = True
    intent: str = ""
    notes: list[str] = []


class TargetMetadataResponse(BaseModel):
    """Audit trail of what the target pipeline actually computed."""

    target_name: str = "forward_log_return_15m"
    horizon_minutes: int = 15
    horizon_bars: int = 15
    bar_minutes: int = 1
    timezone: str = "America/New_York"
    valid_count: int = 0
    total_count: int = 0
    valid_ratio: float = 0.0
    invalid_reason_counts: dict[str, int] = {}


class IcCiResponse(BaseModel):
    """Lo-style confidence interval on the headline mean IC."""

    point: float = 0.0
    se: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_level: float = 0.95
    n_eff_used: float = 0.0
    valid: bool = False
    se_approximation_note: str = ""


class MultipleTestingWarningResponse(BaseModel):
    """Holm-Bonferroni-corrected p-value across the feature family."""

    raw_nw_p_value: float = 1.0
    holm_p_value: float = 1.0
    n_family: int = 0
    note: str = ""


class CostViabilityResponse(BaseModel):
    """Cost-adjusted long-short spread, anchored on spec direction."""

    gross_spread_bps_signed: float = 0.0
    directional_spread_bps: float = 0.0
    cost_assumption_one_way_bps: float = 1.0
    cost_erasure_one_way_bps: float = 0.0
    net_spread_bps_at_assumption: float = 0.0
    viable_at_assumption: bool = False
    spec_direction: str = "unknown"
    note: str = ""


class ValidationScreenResponse(BaseModel):
    """One of the four screens (statistical / economic / OOS / multiple-testing)."""

    name: str = ""
    description: str = ""
    passed: bool = False
    required_for_stage1: bool = False
    failure_reasons: list[str] = []


class FeatureStageCriterionResponse(BaseModel):
    """Single advance-criterion in the next-stage list."""

    name: str = ""
    description: str = ""
    current_value: float = 0.0
    required_repr: str = ""
    met: bool = False


class FeatureStageInfoResponse(BaseModel):
    """Where the feature sits on the 0/1/2/3 ladder."""

    stage: int = 0
    label: str = "Rejected"
    description: str = ""
    next_stage_label: str = ""
    advance_criteria: list[FeatureStageCriterionResponse] = []
    failed_screens: list[str] = []


class FeatureValidationVerdictResponse(BaseModel):
    """Replaces the legacy single-boolean ``passed_validation``."""

    statistical_screen: ValidationScreenResponse
    economic_screen: ValidationScreenResponse
    oos_screen: ValidationScreenResponse
    multiple_testing_screen: ValidationScreenResponse
    regime_stability_screen: ValidationScreenResponse
    multiple_testing: MultipleTestingWarningResponse
    cost_viability: CostViabilityResponse
    ic_ci: IcCiResponse
    direction_matches_spec: bool = True
    target_signed_appropriate: bool = True
    stage_info: FeatureStageInfoResponse
    final_decision: str = ""


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
    feature_spec: FeatureValidationSpecResponse | None = None
    target_metadata: TargetMetadataResponse | None = None
    validation_verdict: FeatureValidationVerdictResponse | None = None
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


class AlphaDecayStatsResponse(BaseModel):
    """Alpha decay regression statistics with power-guard flags."""

    slope: float = 0.0
    intercept: float = 0.0
    t_stat: float = 0.0
    p_value: float = 1.0
    r_squared: float = 0.0
    n_folds_used: int = 0
    is_test_valid: bool = False
    """True only when ``n_folds_used >= 5``. Below that, the regression's
    t-statistic has too few residual degrees of freedom to be informative
    and the UI must render an "insufficient folds" placeholder instead."""
    is_significant: bool = False
    """True when ``is_test_valid`` and ``p_value < 0.05``."""


class SharpeCiResponse(BaseModel):
    """Lo (2002) confidence interval for the annualised Sharpe ratio."""

    point: float = 0.0
    se: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_level: float = 0.95
    n_eff_used: float = 0.0
    valid: bool = False


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
    alpha_decay: AlphaDecayStatsResponse | None = None


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


class Stage0FailureResponse(BaseModel):
    """A single Stage 0 kill criterion that the signal failed."""

    criterion_name: str = ""
    value: float = 0.0
    threshold_repr: str = ""
    message: str = ""


class Stage0RejectionResponse(BaseModel):
    """Stage 0 kill-switch evaluation."""

    rejected: bool = False
    failed_criteria: list[Stage0FailureResponse] = []


class StageAdvanceCriterionResponse(BaseModel):
    """A single requirement to advance from the current stage."""

    name: str = ""
    description: str = ""
    current_value: float = 0.0
    required_repr: str = ""
    met: bool = False


class GraduationStageInfoResponse(BaseModel):
    """Where the signal sits on the 0/1/2/3 ladder, plus advancement criteria."""

    stage: int = 0
    label: str = "Rejected"
    description: str = ""
    next_stage_label: str = ""
    advance_criteria: list[StageAdvanceCriterionResponse] = []


class GraduationResultResponse(BaseModel):
    """Complete graduation assessment."""

    criteria: list[GraduationCriterionResponse] = []
    overall_passed: bool = False
    overall_grade: str = "F"
    summary: str = ""
    status_label: str = "Exploratory"
    parameter_stability: ParameterStabilityResponse | None = None
    stage0_rejection: Stage0RejectionResponse | None = None
    stage_info: GraduationStageInfoResponse | None = None


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
    max_lag_used: int = 0
    rho_sum: float = 0.0


class SignalBehaviorMetricsResponse(BaseModel):
    """Signal behavior analysis on active bars."""

    avg_forward_return_when_active: float = 0.0
    skewness_active_returns: float = 0.0
    avg_win_return: float = 0.0
    avg_loss_return: float = 0.0
    hit_rate: float = 0.0


class DeflatedSharpeResponse(BaseModel):
    """Bailey & López de Prado Deflated Sharpe Ratio for the IS grid headline."""

    raw_sharpe: float = 0.0
    expected_max_under_null: float = 0.0
    dsr_probability: float = 0.0
    n_trials: int = 0
    skewness: float = 0.0
    kurtosis: float = 0.0
    valid: bool = False


class RegimeBucketResponse(BaseModel):
    """One cell of the joint (vol × trend) regime grid."""

    vol_label: str = ""
    trend_label: str = ""
    days: int = 0
    effective_trades: float = 0.0
    badge: str = "Empty"


class MethodologyResponse(BaseModel):
    """Methodology metadata from signal engine configuration."""

    train_months: int = 3
    test_months: int = 1
    window_type: str = "rolling"
    optimization_target: str = "net_sharpe"
    annualization_factor: int = 98280
    bars_per_day: int = 390
    horizon: int = 15
    default_cost_bps: float = 2.0
    min_bars_for_signal: int = 500
    flip_sign: bool = True
    regime_gate_enabled: bool = True
    thresholds: list[float] = []
    cost_bps_options: list[float] = []


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
    """Marginal day counts (legacy). New consumers should prefer ``joint_regime_coverage``."""
    joint_regime_coverage: list[RegimeBucketResponse] = []
    signal_behavior: SignalBehaviorMetricsResponse | None = None
    oos_sharpe_ci: SharpeCiResponse | None = None
    """Lo (2002) confidence interval for the headline OOS Sharpe."""
    deflated_sharpe: DeflatedSharpeResponse | None = None
    """Bailey & López de Prado DSR for the IS grid headline."""
    methodology: MethodologyResponse | None = None
    research_log: str = ""
    error: str | None = None


# ─── Options IV Models ──────────────────────────────────────


class IvDataPoint(BaseModel):
    """Single IV data point for options research."""

    date: str
    atm_iv: float | None = None
    iv_otm_put: float | None = None
    iv_otm_call: float | None = None
    stock_close: float | None = None


class BuildIvHistoryRequest(BaseModel):
    """Request body for POST /research/build-iv-history."""

    underlying_ticker: str = Field(..., description="Stock symbol (e.g. SPY)")
    start_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    end_date: str = Field(..., description="ISO date (YYYY-MM-DD)")


class IvDiagnosticsReportResponse(BaseModel):
    """IV series diagnostics report."""

    valid: bool = False
    missing_pct: float = 0.0
    total_trading_days: int = 0
    valid_iv_days: int = 0
    first_date: str | None = None
    last_date: str | None = None
    gaps: int = 0
    dte_spikes: int = 0
    iv_mean: float | None = None
    iv_std: float | None = None
    iv_min: float | None = None
    iv_max: float | None = None
    iv_skewness: float | None = None
    discontinuities: int = 0
    warnings: list[str] = []


class BuildIvHistoryResponse(BaseModel):
    """Response body for POST /research/build-iv-history."""

    success: bool
    underlying_ticker: str
    start_date: str
    end_date: str
    data_points: int = 0
    iv_data: list[dict] = []
    diagnostics: IvDiagnosticsReportResponse | None = None
    error: str | None = None


class RunOptionsFeatureResearchRequest(BaseModel):
    """Request body for POST /research/run-options-feature."""

    ticker: str = Field(..., description="Stock symbol (e.g. AAPL)")
    feature_name: str = Field(..., description="Options feature (e.g. iv_rank_60)")
    iv_data: list[IvDataPoint] = Field(..., description="Historical IV data")
    stock_daily_bars: list[OHLCVBar] = Field(..., description="Daily stock OHLCV")
    start_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    end_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    target_type: str = Field(default="directional", description="Target: directional, volatility, abs_return")


class RunBatchOptionsRequest(BaseModel):
    """Request body for POST /research/run-batch-options."""

    feature_name: str = Field(..., description="Options feature to test")
    tickers: list[str] = Field(..., description="List of tickers to test")
    start_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    end_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    target_type: str = Field(default="directional", description="Target type")


class TickerBatchResult(BaseModel):
    """Per-ticker result in batch research."""

    ticker: str
    mean_ic: float = 0.0
    ic_t_stat: float = 0.0
    ic_p_value: float = 1.0
    nw_t_stat: float = 0.0
    nw_p_value: float = 1.0
    effective_n: float = 0.0
    is_stationary: bool = False
    passed_validation: bool = False
    data_points: int = 0
    error: str | None = None


class CrossSectionalReportResponse(BaseModel):
    """Response body for POST /research/run-batch-options."""

    success: bool
    feature_name: str
    tickers_tested: int = 0
    tickers_passed: int = 0
    pass_rate: float = 0.0
    cross_sectional_consistent: bool = False
    aggregate_ic: float = 0.0
    ticker_results: list[TickerBatchResult] = []
    summary: str = ""
    error: str | None = None
