"""Records the walk-forward study persistence layer reads and writes (PRD #1925)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Literal

from app.research.grid_search.models import GridSearchSpec

StudyStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
FoldStatus = Literal["pending", "running", "completed", "failed"]


@dataclass(frozen=True)
class StudySpec:
    """The researcher's request: one grid-search spec (grid, range, costs, ranking) plus the two window lengths."""

    grid: GridSearchSpec
    training_months: int
    test_months: int

    def sweep_spec(self, start_ms: int, end_ms: int) -> GridSearchSpec:
        """The same grid, costs and ranking over one fold window."""
        return replace(self.grid, start_ms=start_ms, end_ms=end_ms)

    def as_request_dict(self) -> dict[str, Any]:
        return {**self.grid.as_request_dict(), "training_months": self.training_months, "test_months": self.test_months}

    @classmethod
    def from_request_dict(cls, payload: Mapping[str, Any]) -> StudySpec:
        return cls(grid=GridSearchSpec.from_request_dict(payload), training_months=int(payload["training_months"]), test_months=int(payload["test_months"]))


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
    # Cells the fold's sweeps have recorded (completed or failed); the study's progress is their sum.
    recorded_backtests: int = 0
    failure_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FoldRecord:
        # Tolerate a field this version no longer has: the folds JSON outlives code revisions.
        return cls(**{name: payload[name] for name in _FOLD_FIELDS if name in payload})


_FOLD_FIELDS = tuple(f.name for f in fields(FoldRecord))


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
