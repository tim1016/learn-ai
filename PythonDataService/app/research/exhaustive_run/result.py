"""Persisted DTOs for the SPY EMA Exhaustive Run research artifact."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.runs.result import RunMetrics

ExhaustiveStatus = Literal["completed", "failed"]


class FoldCandidateSelection(BaseModel):
    """One candidate retained from one fold's training-only leaderboard."""

    model_config = ConfigDict(extra="forbid")

    fold_index: int = Field(ge=0)
    fold_rank: int = Field(ge=1, le=5)
    parameters: dict[str, float]
    strategy_spec_hash: str
    train_run_id: str
    train_metrics: RunMetrics
    selection_score: float = Field(ge=0.0, le=1.0)


class CandidateFoldEvidence(BaseModel):
    """One fixed-gap forward fold plus its matching training evidence."""

    model_config = ConfigDict(extra="forbid")

    fold_index: int = Field(ge=0)
    train_start_ms: int = Field(ge=0)
    train_end_ms: int = Field(ge=0)
    test_start_ms: int = Field(ge=0)
    test_end_ms: int = Field(ge=0)
    train_run_id: str
    test_run_id: str | None
    train_metrics: RunMetrics
    test_metrics: RunMetrics
    retention: float | None = None
    status: Literal["completed", "failed"]
    failure_reason: str | None = None


class CandidateForwardStability(BaseModel):
    """Fixed-gap evidence across every canonical forward test fold."""

    model_config = ConfigDict(extra="forbid")

    walk_forward_id: str
    folds: list[CandidateFoldEvidence]
    pct_profitable_folds: float | None = Field(default=None, ge=0.0, le=1.0)
    mean_oos_sharpe: float | None = None
    median_oos_sharpe: float | None = None
    alpha_decay: float | None = None
    mean_fold_retention: float | None = None


class TradeRecencySummary(BaseModel):
    """Completed-trade concentration in the final six study months."""

    model_config = ConfigDict(extra="forbid")

    recent_window_start_ms: int = Field(ge=0)
    recent_window_end_ms: int = Field(ge=0)
    recent_trade_count: int = Field(ge=0)
    prior_trade_count: int = Field(ge=0)
    total_trade_count: int = Field(ge=0)
    recent_trade_share: float | None = Field(default=None, ge=0.0, le=1.0)
    recent_vs_prior_rate_ratio: float | None = Field(default=None, ge=0.0)
    last_trade_exit_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _counts_and_window_are_consistent(self) -> TradeRecencySummary:
        if self.recent_window_start_ms >= self.recent_window_end_ms:
            raise ValueError("recent window start must be before its end")
        if self.recent_trade_count + self.prior_trade_count != self.total_trade_count:
            raise ValueError("recent and prior trades must sum to total trades")
        return self


class ExhaustiveCandidateResult(BaseModel):
    """Full-data and forward-stability evidence for one unique specification."""

    model_config = ConfigDict(extra="forbid")

    parameters: dict[str, float]
    strategy_spec_hash: str
    appearance_count: int = Field(ge=1)
    appearance_fold_indices: list[int]
    best_fold_rank: int = Field(ge=1, le=5)
    latest_selected_fold_index: int = Field(ge=0)
    mean_selection_score: float = Field(ge=0.0, le=1.0)
    status: ExhaustiveStatus
    full_period_run_id: str | None = None
    full_period_metrics: RunMetrics | None = None
    trade_recency: TradeRecencySummary | None = None
    forward_stability: CandidateForwardStability | None = None
    failure_reason: str | None = None


class ExhaustiveRunConfig(BaseModel):
    """Frozen inputs and lineage for an Exhaustive Run artifact."""

    model_config = ConfigDict(extra="forbid")

    exhaustive_run_id: str
    source_walk_forward_id: str
    protocol_id: Literal["spy-ema-exhaustive-run"] = "spy-ema-exhaustive-run"
    protocol_version: Literal["1.0"] = "1.0"
    source_protocol_id: str
    source_protocol_version: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    recent_window_start_ms: int = Field(ge=0)
    max_candidates_per_fold: Literal[5] = 5
    ranking_method: Literal["equal_weight_train_sharpe_return_percentile"] = (
        "equal_weight_train_sharpe_return_percentile"
    )
    initial_cash: float = Field(gt=0, allow_inf_nan=False)
    fill_mode: str
    commission_per_order: float = Field(ge=0, allow_inf_nan=False)
    slippage_per_share: float = Field(ge=0, allow_inf_nan=False)
    random_seed: int
    data_root_revision: str
    created_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _windows_are_ordered(self) -> ExhaustiveRunConfig:
        if not self.start_ms < self.recent_window_start_ms < self.end_ms:
            raise ValueError("recent window must be strictly inside the study window")
        return self


class ExhaustiveRunResult(BaseModel):
    """Sortable result set plus every fold-level selection receipt."""

    model_config = ConfigDict(extra="forbid")

    exhaustive_run_id: str
    source_walk_forward_id: str
    selections: list[FoldCandidateSelection]
    candidates: list[ExhaustiveCandidateResult]
    warnings: list[str] = Field(default_factory=list)
    status: ExhaustiveStatus
    failure_reason: str | None = None
    created_at_ms: int = Field(ge=0)
    completed_at_ms: int = Field(ge=0)
