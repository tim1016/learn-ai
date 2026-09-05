"""HTTP contracts for Walk-Forward Studies (PRD #1925).

A study request is a Grid Search request plus the two window lengths in
whole months; every temporal value on the wire is ``int64 ms UTC``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.grid_search import GridSearchSpecRequest


class WalkForwardStudySpecRequest(GridSearchSpecRequest):
    training_months: int = Field(ge=1, le=120)
    test_months: int = Field(ge=1, le=60)


class WalkForwardStudyJobRequest(WalkForwardStudySpecRequest):
    """Body of POST /api/jobs-internal/walk-forward-study — the spec plus the minted job id.

    ``resume_study_id`` names an incomplete study to Finish instead: the spec
    fields are ignored and the stored request governs.
    """

    job_id: str = Field(min_length=1)
    resume_study_id: str | None = None

    @model_validator(mode="after")
    def _window_is_ordered(self) -> WalkForwardStudyJobRequest:
        # Finish requests carry placeholder spec fields; only a fresh launch needs an ordered window.
        if self.resume_study_id is None and self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be before end_ms")
        return self


class FoldPlanResponse(BaseModel):
    fold_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int


class WalkForwardStudyPreflightResponse(BaseModel):
    strategy_key: str
    symbol: str
    combinations: int
    fold_count: int
    total_backtests: int
    backtest_limit: int
    estimated_seconds: float
    required_samples: int
    run_up_sessions: int
    folds: list[FoldPlanResponse]


class FoldResponse(FoldPlanResponse):
    status: Literal["pending", "running", "completed", "failed"]
    train_search_id: str | None
    test_search_id: str | None
    winner_params_hash: str | None
    winner_params: dict[str, Any] | None
    train_sharpe: float | None
    test_sharpe: float | None
    test_trades: int
    retention: float | None
    failure_reason: str | None


class VerdictResponse(BaseModel):
    label: str
    reason: str
    successful_folds: int
    defined_folds: int
    study_retention: float | None
    median_test_sharpe: float | None
    oos_trade_count: int
    based_on: str


class WalkForwardStudySummaryResponse(BaseModel):
    id: str
    strategy_key: str
    symbol: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    job_id: str | None
    created_at_ms: int
    finished_at_ms: int | None
    window_start_ms: int
    window_end_ms: int
    training_months: int
    test_months: int
    measure: str
    min_trades: int
    fold_count: int
    completed_folds: int
    failed_folds: int
    expected_backtests: int
    completed_backtests: int
    verdict: VerdictResponse | None
    winner_changes: int
    incomplete: bool
    uncommitted_changes: bool
    failure_reason: str | None


class WalkForwardStudyDetailResponse(WalkForwardStudySummaryResponse):
    request: dict[str, Any]
    receipt: dict[str, Any]
    folds: list[FoldResponse]
    resumable: bool
    resume_refusal: str | None
