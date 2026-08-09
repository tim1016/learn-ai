"""Exact-evidence chart contract backed by the policy-keyed bar store."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, JsonValue, model_validator

from app.schemas.chart import ChartIndicatorEntry, ChartIndicatorResult


class EngineChartRequest(BaseModel):
    strategy_name: str = Field(..., min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    symbol: str = Field(..., min_length=1, max_length=20)
    from_ms_utc: int = Field(..., ge=0)
    to_ms_utc: int = Field(..., ge=0)
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
    v: int


class EngineChartCoverage(BaseModel):
    expected_days: int
    available_days: int
    is_complete: bool
    missing_session_ms_utc: list[int] = Field(default_factory=list)


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
