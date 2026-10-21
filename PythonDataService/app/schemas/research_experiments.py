"""HTTP contracts for the Research Lab's experiment history: the legacy rows its screens still read.

These replace the retired GraphQL ``getResearchExperiments`` / ``getSignalExperiments`` /
``getSignalExperimentReport`` queries. Nothing writes the experiment tables any more; a run is started and kept
through the jobs surface. The rows that remain are history.

A signal experiment's report was stored as the JSON text of the Python signal engine's own report
(``_serialize_signal_report`` in ``app/routers/jobs.py``). ``SignalExperimentReport`` and the models under it copy that
content field for field under its stored snake_case names; the only reshaping is that the two ``dict``s keyed by a
number or a label (``regime_coverage``, ``sharpe_values_by_threshold``) are served as lists of entries, as the
GraphQL report served them, because a JSON object key cannot carry a number.

Every report field has the signal engine's own default, so a report stored by an engine that predates a field still
reads. A value of the wrong type does not: it fails the whole report.

Dates inside a report (``start_date``, ``dates``, ``train_start`` ...) are the ``YYYY-MM-DD`` strings the engine
stored; they are served as stored, not converted. Instants (``created_at_ms``) are ``int64 ms UTC``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

TICKER_MAX_LENGTH = 50
# ``"Id"`` is a 32-bit identity column; a larger id names no row, and asking the driver for it would be an error.
MAX_EXPERIMENT_ID = 2_147_483_647

InstantMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]


class ResearchExperimentResponse(BaseModel):
    """One stored feature-research (information-coefficient) experiment."""

    id: int
    ticker: str
    feature_name: str
    start_date: str
    end_date: str
    bars_used: int
    mean_ic: float
    ic_t_stat: float
    ic_p_value: float
    adf_p_value: float
    kpss_p_value: float
    is_stationary: bool
    passed_validation: bool
    monotonicity_ratio: float
    is_monotonic: bool
    created_at_ms: InstantMs


class SignalExperimentResponse(BaseModel):
    """One stored signal-engine experiment: the headline of its report."""

    id: int
    ticker: str
    feature_name: str
    start_date: str
    end_date: str
    bars_used: int
    overall_grade: str
    status_label: str
    overall_passed: bool
    mean_oos_sharpe: float
    best_threshold: float
    best_cost_bps: float
    flip_sign: bool
    regime_gate_enabled: bool
    created_at_ms: InstantMs


# ── The stored report ──────────────────────────────────────────────


def _entries_of(key: str, value_name: str) -> Callable[[Any], Any]:
    """Read a stored ``{key: value}`` object as the list of entries it stands for; anything else is left for validation."""

    def read(stored: Any) -> Any:
        if isinstance(stored, dict):
            return [{key: entry_key, value_name: entry_value} for entry_key, entry_value in stored.items()]
        return stored

    return read


class _StoredReportModel(BaseModel):
    """A part of the stored report: every field defaults on the way in, and every field is present on the way out."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class SignalReportRegimeCount(_StoredReportModel):
    regime: str
    count: int


class SignalReportThresholdSharpe(_StoredReportModel):
    threshold: float
    sharpe: float


class SignalReportBacktest(_StoredReportModel):
    """One threshold-by-cost backtest of the grid."""

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


class SignalReportWalkForwardWindow(_StoredReportModel):
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


class SignalReportAlphaDecay(_StoredReportModel):
    slope: float = 0.0
    intercept: float = 0.0
    t_stat: float = 0.0
    p_value: float = 1.0
    r_squared: float = 0.0
    n_folds_used: int = 0
    is_test_valid: bool = False
    is_significant: bool = False


class SignalReportWalkForward(_StoredReportModel):
    windows: list[SignalReportWalkForwardWindow] = []
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
    alpha_decay: SignalReportAlphaDecay | None = None


class SignalReportGraduationCriterion(_StoredReportModel):
    name: str = ""
    description: str = ""
    passed: bool = False
    value: float = 0.0
    threshold: float = 0.0
    label: str = "Fail"
    failure_reason: str = ""


class SignalReportParameterStability(_StoredReportModel):
    sharpe_values_by_threshold: Annotated[
        list[SignalReportThresholdSharpe], BeforeValidator(_entries_of("threshold", "sharpe"))
    ] = []
    stability_score: float = 0.0
    stability_label: str = "Fragile"


class SignalReportStage0Failure(_StoredReportModel):
    criterion_name: str = ""
    value: float = 0.0
    threshold_repr: str = ""
    message: str = ""


class SignalReportStage0Rejection(_StoredReportModel):
    rejected: bool = False
    failed_criteria: list[SignalReportStage0Failure] = []


class SignalReportStageAdvanceCriterion(_StoredReportModel):
    name: str = ""
    description: str = ""
    current_value: float = 0.0
    required_repr: str = ""
    met: bool = False


class SignalReportStageInfo(_StoredReportModel):
    stage: int = 0
    label: str = "Rejected"
    description: str = ""
    next_stage_label: str = ""
    advance_criteria: list[SignalReportStageAdvanceCriterion] = []


class SignalReportGraduation(_StoredReportModel):
    criteria: list[SignalReportGraduationCriterion] = []
    overall_passed: bool = False
    overall_grade: str = "F"
    summary: str = ""
    status_label: str = "Exploratory"
    parameter_stability: SignalReportParameterStability | None = None
    stage0_rejection: SignalReportStage0Rejection | None = None
    stage_info: SignalReportStageInfo | None = None


class SignalReportDiagnostics(_StoredReportModel):
    signal_mean: float = 0.0
    signal_std: float = 0.0
    pct_time_active: float = 0.0
    avg_abs_signal: float = 0.0
    pct_filtered_by_threshold: float = 0.0
    pct_gated_by_regime: float = 0.0


class SignalReportDataSufficiency(_StoredReportModel):
    total_bars: int = 0
    train_bars: int = 0
    test_bars: int = 0
    walk_forward_folds: int = 0
    effective_oos_bars: int = 0
    regimes_covered: int = 0
    regime_coverage: Annotated[list[SignalReportRegimeCount], BeforeValidator(_entries_of("regime", "count"))] = []
    coverage_warnings: list[str] = []


class SignalReportEffectiveSample(_StoredReportModel):
    raw_n: int = 0
    effective_n: float = 0.0
    autocorrelation_lag1: float = 0.0
    independent_bets: int = 0
    max_lag_used: int = 0
    rho_sum: float = 0.0


class SignalReportRegimeBucket(_StoredReportModel):
    vol_label: str = ""
    trend_label: str = ""
    days: int = 0
    effective_trades: float = 0.0
    badge: str = "Empty"


class SignalReportBehavior(_StoredReportModel):
    avg_forward_return_when_active: float = 0.0
    skewness_active_returns: float = 0.0
    avg_win_return: float = 0.0
    avg_loss_return: float = 0.0
    hit_rate: float = 0.0


class SignalReportSharpeCi(_StoredReportModel):
    point: float = 0.0
    se: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_level: float = 0.95
    n_eff_used: float = 0.0
    valid: bool = False


class SignalReportDeflatedSharpe(_StoredReportModel):
    raw_sharpe: float = 0.0
    expected_max_under_null: float = 0.0
    dsr_probability: float = 0.0
    n_trials: int = 0
    skewness: float = 0.0
    kurtosis: float = 0.0
    valid: bool = False


class SignalReportMethodology(_StoredReportModel):
    train_months: int = 0
    test_months: int = 0
    window_type: str = "rolling"
    optimization_target: str = "net_sharpe"
    annualization_factor: int = 0
    bars_per_day: int = 0
    horizon: int = 0
    default_cost_bps: float = 0.0
    min_bars_for_signal: int = 0
    flip_sign: bool = False
    regime_gate_enabled: bool = False
    thresholds: list[float] = []
    cost_bps_options: list[float] = []


class SignalExperimentReport(_StoredReportModel):
    """The signal engine's full report for one stored experiment."""

    success: bool = False
    ticker: str = ""
    feature_name: str = ""
    start_date: str = ""
    end_date: str = ""
    bars_used: int = 0
    flip_sign: bool = True
    thresholds_tested: list[float] = []
    cost_bps_options: list[float] = []
    best_threshold: float = 0.0
    best_cost_bps: float = 0.0
    backtest_grid: list[SignalReportBacktest] = []
    walk_forward: SignalReportWalkForward | None = None
    graduation: SignalReportGraduation | None = None
    signal_diagnostics: SignalReportDiagnostics | None = None
    data_sufficiency: SignalReportDataSufficiency | None = None
    effective_sample: SignalReportEffectiveSample | None = None
    regime_coverage: Annotated[list[SignalReportRegimeCount], BeforeValidator(_entries_of("regime", "count"))] = []
    joint_regime_coverage: list[SignalReportRegimeBucket] = []
    signal_behavior: SignalReportBehavior | None = None
    oos_sharpe_ci: SignalReportSharpeCi | None = None
    deflated_sharpe: SignalReportDeflatedSharpe | None = None
    methodology: SignalReportMethodology | None = None
    research_log: str = ""
    error: str | None = None


class ResearchExperimentRefusal(BaseModel):
    code: str
    message: str


class ResearchExperimentRefusalBody(BaseModel):
    """The 404 / 500 body: FastAPI wraps an ``HTTPException`` detail as ``{"detail": ...}``."""

    detail: ResearchExperimentRefusal
