"""Records the Grid Search persistence layer reads and writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SearchStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
# ``interrupted`` is never stored: it is what a ``running`` row reads back as
# when no live job backs it (PRD #1926 "Lifecycle and persistence").
PresentedStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
CellStatus = Literal["completed", "failed"]
OwnerKind = Literal["user", "walk_forward"]

CELL_SORT_COLUMNS: tuple[str, ...] = (
    "sharpe_ratio",
    "total_return_pct",
    "net_profit",
    "total_trades",
    "max_drawdown_pct",
    "win_rate",
    "params_hash",
)


@dataclass(frozen=True)
class SearchOwner:
    kind: OwnerKind = "user"
    owner_id: str | None = None
    fold_index: int | None = None
    phase: str | None = None


@dataclass(frozen=True)
class NewSearch:
    """What is durable the moment a search is launched, before any cell runs."""

    id: str
    strategy_key: str
    symbol: str
    request: dict[str, Any]
    receipt: dict[str, Any]
    expected_cells: int
    job_id: str | None
    owner: SearchOwner = field(default_factory=SearchOwner)


@dataclass(frozen=True)
class CellResult:
    """One completed or failed cell, as the runner hands it to persistence."""

    params_hash: str
    params: dict[str, Any]
    status: CellStatus
    total_trades: int = 0
    net_profit: float | None = None
    total_return_pct: float | None = None
    sharpe_ratio: float | None = None
    max_drawdown_pct: float | None = None
    win_rate: float | None = None
    bars_consumed: int | None = None
    error: str | None = None
    exploratory: bool = False


@dataclass(frozen=True)
class CellRow(CellResult):
    search_id: str = ""
    attempt: int = 0
    completed_at_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchRow:
    id: str
    owner: SearchOwner
    strategy_key: str
    symbol: str
    status: SearchStatus
    attempt: int
    job_id: str | None
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None
    request: dict[str, Any]
    receipt: dict[str, Any]
    expected_cells: int
    completed_cells: int
    failed_cells: int
    leader_params_hash: str | None
    incomplete: bool
    failure_reason: str | None


@dataclass(frozen=True)
class CellPage:
    total: int
    page: int
    page_size: int
    cells: list[CellRow]
