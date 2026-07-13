"""Wire contracts for Engine Lab validation analytics.

The models in this module are transport-only. Numerical ownership lives in
``app.services.engine_validation_analytics``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PerformanceHorizonResponse(BaseModel):
    key: Literal["2w", "1m", "3m", "6m", "1y", "2y"]
    label: str
    start_ms_utc: int
    end_ms_utc: int
    has_full_coverage: bool
    net_return: float | None = None
    trade_count: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None


class TimingCellResponse(BaseModel):
    weekday: int = Field(..., ge=0, le=4)
    weekday_label: str
    hour_et: int = Field(..., ge=0, le=23)
    trade_count: int = Field(..., ge=1)
    win_rate: float
    average_return: float


class SeasonalityMonthResponse(BaseModel):
    month: int = Field(..., ge=1, le=12)
    month_label: str
    observation_count: int = Field(..., ge=0)
    median_compounded_return: float | None = None


class RollingTradePointResponse(BaseModel):
    trade_number: int = Field(..., ge=1)
    end_ms_utc: int
    window_size: int = Field(..., ge=1)
    average_return: float
    win_rate: float


class EngineValidationAnalyticsResponse(BaseModel):
    horizons: list[PerformanceHorizonResponse] = Field(default_factory=list)
    timing_cells: list[TimingCellResponse] = Field(default_factory=list)
    seasonality: list[SeasonalityMonthResponse] = Field(default_factory=list)
    rolling_trade_stability: list[RollingTradePointResponse] = Field(default_factory=list)

