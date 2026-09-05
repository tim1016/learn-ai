"""HTTP contracts for Grid Search (PRD #1926).

Every temporal value on the wire is ``int64 ms UTC`` (temporal-rigor.md).
The researcher's window is half-open ``[start_ms, end_ms)``; the engine
boundary takes inclusive ET trading dates, and one rule converts between
them (``app.research.grid_search.service.window_dates``): the start date is
the ET calendar date of ``start_ms`` and the end date is the ET calendar
date of the last millisecond before ``end_ms``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.research.grid_search.models import GridSearchSpec
from app.research.sweep.grid import LowHighStepRange, ParamRange, ValueListRange
from app.research.sweep.ranking import RankingMeasure

SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,11}$"
FillModeName = Literal["signal_bar_close", "next_bar_open"]


class _CamelTolerantModel(BaseModel):
    """Accepts camelCase (the .NET jobs passthrough) and snake_case (direct FastAPI calls)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ValueListRangeRequest(_CamelTolerantModel):
    type: Literal["value_list"] = "value_list"
    values: list[float] = Field(min_length=1)


class LowHighStepRangeRequest(_CamelTolerantModel):
    type: Literal["low_high_step"] = "low_high_step"
    low: float
    high: float
    step: float


ParamRangeRequest = Annotated[ValueListRangeRequest | LowHighStepRangeRequest, Field(discriminator="type")]


class GridSearchSpecRequest(_CamelTolerantModel):
    """What the researcher asks for — the same body for preflight and launch."""

    strategy_key: str = Field(min_length=1)
    # Upper-cased before the pattern runs; a letter first and no separators keeps the
    # symbol a single lake path component (no ``/``, no ``..``).
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    param_ranges: dict[str, ParamRangeRequest] = Field(default_factory=dict)
    start_ms: int = Field(ge=0, description="Half-open window start, int64 ms UTC")
    end_ms: int = Field(ge=0, description="Half-open window end, int64 ms UTC")
    resolution: Literal["minute", "daily"] = "minute"
    fill_mode: FillModeName = "signal_bar_close"
    commission_per_order: float = Field(1.0, ge=0)
    slippage_per_share: float = Field(0.0, ge=0)
    initial_cash: float = Field(100_000.0, gt=0)
    measure: RankingMeasure = "sharpe_ratio"
    min_trades: int = Field(5, ge=1)

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _window_is_ordered(self) -> GridSearchSpecRequest:
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be before end_ms")
        return self


def to_param_range(spec: ValueListRangeRequest | LowHighStepRangeRequest) -> ParamRange:
    if isinstance(spec, ValueListRangeRequest):
        return ValueListRange(tuple(spec.values))
    return LowHighStepRange(low=spec.low, high=spec.high, step=spec.step)


def to_grid_spec(body: GridSearchSpecRequest) -> GridSearchSpec:
    """The parsed request as the service's spec; the symbol is already normalized by validation."""
    return GridSearchSpec(
        strategy_key=body.strategy_key,
        symbol=body.symbol,
        param_ranges={name: to_param_range(spec) for name, spec in body.param_ranges.items()},
        start_ms=body.start_ms,
        end_ms=body.end_ms,
        resolution=body.resolution,
        fill_mode=body.fill_mode,
        commission_per_order=body.commission_per_order,
        slippage_per_share=body.slippage_per_share,
        initial_cash=body.initial_cash,
        measure=body.measure,
        min_trades=body.min_trades,
    )


class GridSearchJobRequest(GridSearchSpecRequest):
    """Body of POST /api/jobs-internal/grid-search — the spec plus the minted job id.

    ``resume_search_id`` names an incomplete search to Finish instead: the
    spec fields are ignored and the stored request governs.
    """

    job_id: str = Field(min_length=1)
    resume_search_id: str | None = None


class RunUpPlanResponse(BaseModel):
    data_start_ms: int
    evaluation_start_ms: int
    evaluation_end_ms: int
    required_samples: int
    bar_span_ms: int
    run_up_sessions: int
    carved_from_range: bool


class GridSearchPreflightResponse(BaseModel):
    strategy_key: str
    symbol: str
    combinations: int
    total_backtests: int
    backtest_limit: int
    estimated_seconds: float
    run_up: RunUpPlanResponse
    expected_sessions: int


class SearchOwnerResponse(BaseModel):
    kind: Literal["user", "walk_forward"]
    owner_id: str | None
    fold_index: int | None
    phase: str | None


class GridSearchSummaryResponse(BaseModel):
    """The history row: enough to judge a search without opening it."""

    id: str
    owner: SearchOwnerResponse
    strategy_key: str
    symbol: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    job_id: str | None
    created_at_ms: int
    finished_at_ms: int | None
    window_start_ms: int
    window_end_ms: int
    measure: RankingMeasure
    min_trades: int
    expected_cells: int
    completed_cells: int
    failed_cells: int
    leader_params_hash: str | None
    leader_params: dict[str, Any] | None
    incomplete: bool
    uncommitted_changes: bool
    failure_reason: str | None


class GridSearchDetailResponse(GridSearchSummaryResponse):
    request: dict[str, Any]
    receipt: dict[str, Any]
    resumable: bool
    resume_refusal: str | None


class GridSearchCellResponse(BaseModel):
    params_hash: str
    params: dict[str, Any]
    status: Literal["completed", "failed"]
    attempt: int
    total_trades: int
    net_profit: float | None
    total_return_pct: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float | None
    win_rate: float | None
    bars_consumed: int | None
    error: str | None
    exploratory: bool
    completed_at_ms: int
    is_leader: bool
    eligible: bool


class GridSearchCellPageResponse(BaseModel):
    total: int
    page: int
    page_size: int
    sort_by: str
    direction: Literal["asc", "desc"]
    cells: list[GridSearchCellResponse]
