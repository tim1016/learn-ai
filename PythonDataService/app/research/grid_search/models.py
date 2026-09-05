"""Records the Grid Search persistence layer reads and writes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.engine.data.availability import Resolution
from app.research.sweep.grid import LowHighStepRange, ParamRange, ValueListRange
from app.research.sweep.ranking import RankingMeasure

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


@dataclass(frozen=True, kw_only=True)
class CellRow(CellResult):
    search_id: str
    attempt: int
    completed_at_ms: int

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
    leader_params: dict[str, Any] | None
    incomplete: bool
    failure_reason: str | None


@dataclass(frozen=True)
class CellPage:
    total: int
    page: int
    page_size: int
    cells: list[CellRow]


# ── Spec ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GridSearchSpec:
    """The researcher's request, already parsed into the grid vocabulary."""

    strategy_key: str
    symbol: str
    param_ranges: Mapping[str, ParamRange]
    start_ms: int
    end_ms: int
    resolution: Resolution = "minute"
    fill_mode: str = "signal_bar_close"
    commission_per_order: float = 1.0
    slippage_per_share: float = 0.0
    initial_cash: float = 100_000.0
    measure: RankingMeasure = "sharpe_ratio"
    min_trades: int = 5

    def as_request_dict(self) -> dict[str, Any]:
        return {
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "param_ranges": {name: _range_to_dict(spec) for name, spec in sorted(self.param_ranges.items())},
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "resolution": self.resolution,
            "fill_mode": self.fill_mode,
            "commission_per_order": self.commission_per_order,
            "slippage_per_share": self.slippage_per_share,
            "initial_cash": self.initial_cash,
            "measure": self.measure,
            "min_trades": self.min_trades,
        }

    @classmethod
    def from_request_dict(cls, payload: Mapping[str, Any]) -> GridSearchSpec:
        return cls(
            strategy_key=payload["strategy_key"],
            symbol=payload["symbol"],
            param_ranges={name: _range_from_dict(spec) for name, spec in payload["param_ranges"].items()},
            start_ms=int(payload["start_ms"]),
            end_ms=int(payload["end_ms"]),
            resolution=payload.get("resolution", "minute"),
            fill_mode=payload.get("fill_mode", "signal_bar_close"),
            commission_per_order=float(payload.get("commission_per_order", 1.0)),
            slippage_per_share=float(payload.get("slippage_per_share", 0.0)),
            initial_cash=float(payload.get("initial_cash", 100_000.0)),
            measure=payload.get("measure", "sharpe_ratio"),
            min_trades=int(payload.get("min_trades", 5)),
        )


def _range_to_dict(spec: ParamRange) -> dict[str, Any]:
    if isinstance(spec, ValueListRange):
        return {"type": "value_list", "values": list(spec.values)}
    return {"type": "low_high_step", "low": spec.low, "high": spec.high, "step": spec.step}


def _range_from_dict(payload: Mapping[str, Any]) -> ParamRange:
    if payload["type"] == "value_list":
        return ValueListRange(tuple(float(v) for v in payload["values"]))
    return LowHighStepRange(low=float(payload["low"]), high=float(payload["high"]), step=float(payload["step"]))
