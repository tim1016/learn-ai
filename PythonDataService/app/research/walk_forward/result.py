"""Pydantic DTOs for walk-forward results.

Mirrors the Phase A pattern: every timestamp is ``int64 ms UTC``,
``Decimal`` is cast to ``float`` at the wire boundary, ``extra='forbid'``
makes schema drift loud. Identity columns (``walk_forward_id``,
``parent_run_id``, ``spec_hash``) are stable across reruns of the
same configuration.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.research.runs.result import EquityCurvePoint, RunMetrics


class SplitPolicySpec(BaseModel):
    """Wire-shape for a split policy. ``kind`` discriminates; the rest of
    the fields are policy-specific (validated again in ``splits.py``).
    """

    model_config = ConfigDict(extra="allow")

    kind: Literal["chronological", "rolling", "anchored"]


class FoldResult(BaseModel):
    """One fold's contribution to a walk-forward run.

    ``test_run_id`` points at the persisted child run under
    ``artifacts/runs/<test_run_id>/`` — clients can fetch the full
    ``BacktestRunResult`` (equity curve, trades, log) via the existing
    ``GET /api/research/strategy-runs/{run_id}`` endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    fold_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    test_run_id: str
    test_metrics: RunMetrics
    test_trade_count: int
    # ``selected_parameters`` is empty under Phase 4A (fixed spec) and
    # reserved for Phase 4B (parameter selection on train, frozen on
    # test). Surfacing it now means the client never has to handle
    # "missing field" later.
    selected_parameters: dict = Field(default_factory=dict)


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
    created_at_ms: int


class WalkForwardResult(BaseModel):
    """Aggregated walk-forward output.

    The ``combined_oos_equity_curve`` is **compounded** across folds
    (fold N's start equity = fold N-1's end equity), which models the
    investor experience of holding through the strategy across all
    test windows. Rebased-per-fold is a future toggle if needed.
    """

    model_config = ConfigDict(extra="forbid")

    walk_forward_id: str
    parent_run_id: str | None = None
    strategy_spec_hash: str
    split_policy: SplitPolicySpec
    folds: list[FoldResult] = Field(default_factory=list)
    combined_oos_equity_curve: list[EquityCurvePoint] = Field(default_factory=list)
    mean_oos_sharpe: float | None = None
    median_oos_sharpe: float | None = None
    pct_profitable_folds: float | None = None
    oos_retention: float | None = None
    alpha_decay: float | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at_ms: int
    completed_at_ms: int | None = None
    status: Literal["completed", "failed"] = "completed"
    failure_reason: str | None = None
