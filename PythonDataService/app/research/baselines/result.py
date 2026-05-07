"""Pydantic DTOs for null-baseline configurations and results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.research.runs.result import RunMetrics

BaselineMethodLiteral = Literal["buy_and_hold", "random_ema_windows"]


class BaselineRunRecord(BaseModel):
    """One null-run's contribution.

    ``baseline_run_id`` is the persisted child ``RunLedger`` — clients
    can fetch the full ``BacktestRunResult`` (equity curve, trades)
    via ``GET /api/research/strategy-runs/{baseline_run_id}``.
    Failed baselines (engine refused the spec, infrastructure error)
    appear here with ``status='failed'`` and ``failure_reason`` set;
    aggregation skips them.
    """

    model_config = ConfigDict(extra="forbid")

    baseline_run_id: str
    method: BaselineMethodLiteral
    parameters: dict = Field(
        default_factory=dict,
        description=(
            "Method-specific sampled parameters. ``buy_and_hold`` is "
            "parameter-less so this is empty; ``random_ema_windows`` "
            "stores ``{'fast': int, 'slow': int}``."
        ),
    )
    test_metrics: RunMetrics
    test_trade_count: int
    status: Literal["completed", "failed"] = "completed"
    failure_reason: str | None = None


class NullDistribution(BaseModel):
    """Per-metric null distribution + parent's empirical position.

    ``empirical_percentile`` is the fraction of *successful* baseline
    runs whose metric value is **strictly less than** the parent's
    value. For higher-is-better metrics (Sharpe, total_return_pct),
    higher percentile = parent did better than the null. For
    lower-is-better metrics (max_drawdown_pct), higher percentile =
    parent did *worse* — the user reads percentiles knowing each
    metric's directionality (which is documented in
    ``RunMetrics`` itself).

    ``empirical_p_value`` is the small-sample p-value for "parent is
    anomalously high vs the null":
    ``(1 + count(null >= parent)) / (N + 1)``. The +1 / +1 add-one
    correction (Phipson & Smyth 2010) keeps the estimator unbiased
    for finite N. For lower-is-better metrics, the symmetric form
    is ``1 - empirical_p_value`` — clients compute that themselves.
    """

    model_config = ConfigDict(extra="forbid")

    metric_name: str
    parent_value: float | None
    null_values: list[float] = Field(default_factory=list)
    empirical_percentile: float | None = None
    empirical_p_value: float | None = None


class BaselineConfig(BaseModel):
    """Persistable record of the inputs that produced a baseline result."""

    model_config = ConfigDict(extra="forbid")

    baseline_id: str
    parent_run_id: str
    parent_trade_log_hash: str
    method: BaselineMethodLiteral
    sample_count: int = Field(ge=1)
    random_seed: int = Field(ge=0)
    method_params: dict = Field(
        default_factory=dict,
        description=(
            "Method-specific configuration that's not per-baseline. "
            "``random_ema_windows`` stores ``{fast_range, slow_range}``; "
            "``buy_and_hold`` is empty."
        ),
    )
    target_metrics: list[str] = Field(
        default_factory=list,
        description="RunMetrics field names the null distribution covers",
    )
    created_at_ms: int


class BaselineResult(BaseModel):
    """Aggregated null-baseline output."""

    model_config = ConfigDict(extra="forbid")

    baseline_id: str
    parent_run_id: str
    method: BaselineMethodLiteral
    sample_count: int
    baselines: list[BaselineRunRecord] = Field(default_factory=list)
    null_distributions: list[NullDistribution] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at_ms: int
    completed_at_ms: int | None = None
    status: Literal["completed", "failed"] = "completed"
    failure_reason: str | None = None
