"""Pydantic DTOs for walk-forward results.

Mirrors the Phase A pattern: every timestamp is ``int64 ms UTC``,
``Decimal`` is cast to ``float`` at the wire boundary, ``extra='forbid'``
makes schema drift loud. Identity columns (``walk_forward_id``,
``parent_run_id``, ``spec_hash``) are stable across reruns of the
same configuration.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.research.runs.result import EquityCurvePoint, RunMetrics


class ChronologicalSplitPolicySpec(BaseModel):
    """Strict HTTP/storage shape for one chronological split."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["chronological"]
    train_pct: float = Field(gt=0, lt=1, strict=True, allow_inf_nan=False)


class RollingSplitPolicySpec(BaseModel):
    """Strict HTTP/storage shape for one rolling split."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["rolling"]
    train_days: int = Field(gt=0, strict=True)
    test_days: int = Field(gt=0, strict=True)
    step_days: int = Field(gt=0, strict=True)


class AnchoredSplitPolicySpec(BaseModel):
    """Strict HTTP/storage shape for one anchored split."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["anchored"]
    initial_train_days: int = Field(gt=0, strict=True)
    test_days: int = Field(gt=0, strict=True)
    step_days: int = Field(gt=0, strict=True)


SplitPolicySpec = Annotated[
    ChronologicalSplitPolicySpec | RollingSplitPolicySpec | AnchoredSplitPolicySpec,
    Field(discriminator="kind"),
]
_SPLIT_POLICY_ADAPTER = TypeAdapter(SplitPolicySpec)


def parse_split_policy_spec(value: object) -> SplitPolicySpec:
    """Validate untrusted split JSON through the discriminated union."""
    return _SPLIT_POLICY_ADAPTER.validate_python(value)


class ParameterCandidateConfig(BaseModel):
    """One fully-materialized strategy candidate in a train-side grid."""

    model_config = ConfigDict(extra="forbid")

    parameters: dict[str, float]
    strategy_spec_hash: str
    strategy_spec_json: dict


class ParameterSearchConfig(BaseModel):
    """Persisted, replayable definition of train-side model selection."""

    model_config = ConfigDict(extra="forbid")

    objective: Literal["sharpe_ratio"] = "sharpe_ratio"
    min_trades: int = Field(ge=1)
    candidates: list[ParameterCandidateConfig] = Field(min_length=2)


class TrainingCandidateResult(BaseModel):
    """Auditable train-window outcome for one parameter candidate."""

    model_config = ConfigDict(extra="forbid")

    parameters: dict[str, float]
    train_run_id: str
    train_metrics: RunMetrics
    eligible: bool
    receipt_persisted: bool = True
    ineligibility_reason: str | None = None


class FoldResult(BaseModel):
    """One fold's contribution to a walk-forward run.

    ``test_run_id`` points at the persisted child run under
    ``artifacts/runs/<test_run_id>/`` — clients can fetch the full
    ``BacktestRunResult`` (equity curve, trades, log) via the existing
    ``GET /api/research/strategy-runs/{run_id}`` endpoint.

    ``status`` mirrors the underlying ``RunLedger.status`` for this
    fold's test run. Aggregation metrics (``pct_profitable_folds``,
    ``mean_oos_sharpe``) use this field to exclude failed folds from
    the OOS scoreboard — a fold that crashed at the engine boundary
    is an infrastructure problem, not a strategy story.
    """

    model_config = ConfigDict(extra="forbid")

    fold_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    test_run_id: str | None
    test_metrics: RunMetrics
    test_trade_count: int
    status: Literal["completed", "failed"] = "completed"
    failure_reason: str | None = None
    selected_parameters: dict[str, float] = Field(default_factory=dict)
    training_candidates: list[TrainingCandidateResult] = Field(default_factory=list)
    selected_train_sharpe: float | None = None
    oos_retention: float | None = None


class SelectionFailureResult(BaseModel):
    """Train-side evidence retained when a fold cannot select a winner."""

    model_config = ConfigDict(extra="forbid")

    fold_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    training_candidates: list[TrainingCandidateResult] = Field(default_factory=list)
    failure_reason: str


class WalkForwardConfig(BaseModel):
    """Persistable record of the *inputs* that produced a walk-forward
    result. Lets the storage layer round-trip the request without
    consulting the result (e.g., to re-run with different cost
    assumptions while keeping the split fixed).
    """

    model_config = ConfigDict(extra="forbid")

    walk_forward_id: str
    parent_run_id: str | None = None
    strategy_spec_hash: str
    strategy_spec_json: dict
    symbol: str
    resolution_minutes: int
    start_ms: int
    end_ms: int
    initial_cash: float
    fill_mode: str
    commission_per_order: float
    slippage_per_share: float
    random_seed: int
    split_policy: SplitPolicySpec
    parameter_search: ParameterSearchConfig | None = None
    fold_position_policy: Literal["flat_at_test_boundaries"] = "flat_at_test_boundaries"
    protocol_id: str | None = None
    protocol_version: str | None = None
    created_at_ms: int

    @model_validator(mode="after")
    def _protocol_identity_is_complete(self) -> WalkForwardConfig:
        if (self.protocol_id is None) != (self.protocol_version is None):
            raise ValueError("protocol_id and protocol_version must be provided together")
        return self


class WalkForwardResult(BaseModel):
    """Aggregated walk-forward output.

    The ``combined_oos_equity_curve`` compounds independently-flat OOS
    fold returns. Indicator state is pre-rolled from each fold's train
    window, but positions never cross an OOS boundary.
    """

    model_config = ConfigDict(extra="forbid")

    walk_forward_id: str
    parent_run_id: str | None = None
    strategy_spec_hash: str
    split_policy: SplitPolicySpec
    folds: list[FoldResult] = Field(default_factory=list)
    selection_failures: list[SelectionFailureResult] = Field(default_factory=list)
    combined_oos_equity_curve: list[EquityCurvePoint] = Field(default_factory=list)
    mean_oos_sharpe: float | None = None
    median_oos_sharpe: float | None = None
    pct_profitable_folds: float | None = None
    oos_retention: float | None = None
    oos_retention_basis: (
        Literal[
            "mean_fold_test_to_selected_train",
            "mean_oos_to_parent",
        ]
        | None
    ) = None
    alpha_decay: float | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at_ms: int
    completed_at_ms: int | None = None
    status: Literal["completed", "failed"] = "completed"
    failure_reason: str | None = None
