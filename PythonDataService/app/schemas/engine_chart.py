"""Exact-evidence chart contract backed by the policy-keyed bar store."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, model_validator

from app.schemas.chart import ChartIndicatorEntry, ChartIndicatorResult
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class EngineChartRequest(BaseModel):
    strategy_name: str = Field(..., min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    symbol: str = Field(..., min_length=1, max_length=20)
    from_ms_utc: int = Field(..., ge=0, le=MAX_TIMESTAMP_MS)
    to_ms_utc: int = Field(..., ge=0, le=MAX_TIMESTAMP_MS)
    adjusted: bool = True
    session: Literal["regular", "extended"] = "regular"
    timespan: Literal["minute", "hour", "day"] = "minute"
    multiplier: int = Field(1, ge=1)
    indicators: list[ChartIndicatorEntry] = Field(default_factory=list)
    excluded_strategy_indicator_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_window(self) -> EngineChartRequest:
        if self.to_ms_utc <= self.from_ms_utc:
            raise ValueError("to_ms_utc must be greater than from_ms_utc")
        return self


class EngineChartBar(BaseModel):
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


class EngineChartCoverage(BaseModel):
    expected_days: int
    available_days: int
    is_complete: bool
    missing_session_ms_utc: list[Annotated[int, Field(le=MAX_TIMESTAMP_MS)]] = Field(default_factory=list)


class ResolvedChartIndicator(BaseModel):
    id: str
    name: str
    params: dict[str, int | float]
    label: str
    strategy_default: bool


class EngineChartResponse(BaseModel):
    policy_key: str
    symbol: str
    bars: list[EngineChartBar]
    coverage: EngineChartCoverage
    indicator_specs: list[ResolvedChartIndicator]
    indicators: list[ChartIndicatorResult]


class EngineStrategyViewRequest(BaseModel):
    """One backtest's strategy view (#2639 D13): its settings, its window and its warmup.

    The window and warmup are NYSE session-open anchors, as on the engine
    chart: ``from_ms_utc`` opens the first evaluated session, ``to_ms_utc``
    the session after the last, ``warmup_from_ms_utc`` the run's warmup start
    when it read history before its window.
    """

    strategy_name: str = Field(..., min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    symbol: str = Field(..., min_length=1, max_length=20)
    from_ms_utc: int = Field(..., ge=0, le=MAX_TIMESTAMP_MS)
    to_ms_utc: int = Field(..., ge=0, le=MAX_TIMESTAMP_MS)
    warmup_from_ms_utc: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    adjusted: bool = True
    session: Literal["regular", "extended"] = "regular"

    @model_validator(mode="after")
    def validate_window(self) -> EngineStrategyViewRequest:
        if self.to_ms_utc <= self.from_ms_utc:
            raise ValueError("to_ms_utc must be greater than from_ms_utc")
        if self.warmup_from_ms_utc is not None and self.warmup_from_ms_utc > self.from_ms_utc:
            raise ValueError("warmup_from_ms_utc must not be later than from_ms_utc")
        return self
