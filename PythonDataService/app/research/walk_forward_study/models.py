"""Records the walk-forward study persistence layer reads and writes (PRD #1925)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.research.grid_search.service import GridSearchSpec
from app.research.sweep.grid import ParamRange
from app.research.sweep.ranking import RankingMeasure

StudyStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
FoldStatus = Literal["pending", "running", "completed", "failed"]
FoldPhase = Literal["train", "test"]


@dataclass(frozen=True)
class StudySpec:
    """The researcher's request: a grid-search spec plus the two window lengths."""

    strategy_key: str
    symbol: str
    param_ranges: Mapping[str, ParamRange]
    start_ms: int
    end_ms: int
    training_months: int
    test_months: int
    resolution: Literal["minute", "daily"] = "minute"
    fill_mode: str = "signal_bar_close"
    commission_per_order: float = 1.0
    slippage_per_share: float = 0.0
    initial_cash: float = 100_000.0
    measure: RankingMeasure = "sharpe_ratio"
    min_trades: int = 5

    def sweep_spec(self, start_ms: int, end_ms: int) -> GridSearchSpec:
        """The grid-search spec for one window of this study — same grid, costs and ranking."""
        return GridSearchSpec(
            strategy_key=self.strategy_key,
            symbol=self.symbol,
            param_ranges=dict(self.param_ranges),
            start_ms=start_ms,
            end_ms=end_ms,
            resolution=self.resolution,
            fill_mode=self.fill_mode,
            commission_per_order=self.commission_per_order,
            slippage_per_share=self.slippage_per_share,
            initial_cash=self.initial_cash,
            measure=self.measure,
            min_trades=self.min_trades,
        )

    def as_request_dict(self) -> dict[str, Any]:
        base = self.sweep_spec(self.start_ms, self.end_ms).as_request_dict()
        return {**base, "training_months": self.training_months, "test_months": self.test_months}

    @classmethod
    def from_request_dict(cls, payload: Mapping[str, Any]) -> StudySpec:
        sweep = GridSearchSpec.from_request_dict(payload)
        return cls(
            strategy_key=sweep.strategy_key,
            symbol=sweep.symbol,
            param_ranges=sweep.param_ranges,
            start_ms=sweep.start_ms,
            end_ms=sweep.end_ms,
            training_months=int(payload["training_months"]),
            test_months=int(payload["test_months"]),
            resolution=sweep.resolution,
            fill_mode=sweep.fill_mode,
            commission_per_order=sweep.commission_per_order,
            slippage_per_share=sweep.slippage_per_share,
            initial_cash=sweep.initial_cash,
            measure=sweep.measure,
            min_trades=sweep.min_trades,
        )


@dataclass(frozen=True)
class FoldRecord:
    """One fold's durable state; windows are ET-midnight anchors with exclusive ends."""

    fold_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    status: FoldStatus = "pending"
    train_search_id: str | None = None
    test_search_id: str | None = None
    winner_params_hash: str | None = None
    winner_params: dict[str, Any] | None = None
    train_sharpe: float | None = None
    test_sharpe: float | None = None
    test_trades: int = 0
    retention: float | None = None
    failure_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FoldRecord:
        return cls(**payload)


@dataclass(frozen=True)
class NewStudy:
    id: str
    strategy_key: str
    symbol: str
    request: dict[str, Any]
    receipt: dict[str, Any]
    folds: list[FoldRecord]
    expected_backtests: int
    job_id: str | None


@dataclass(frozen=True)
class StudyRow:
    id: str
    strategy_key: str
    symbol: str
    status: StudyStatus
    attempt: int
    job_id: str | None
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None
    request: dict[str, Any]
    receipt: dict[str, Any]
    folds: list[FoldRecord] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    expected_backtests: int = 0
    completed_backtests: int = 0
    incomplete: bool = False
    failure_reason: str | None = None
