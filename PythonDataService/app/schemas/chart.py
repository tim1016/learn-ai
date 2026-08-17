"""Shared request and response contracts for chart indicator computation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class ChartIndicatorEntry(BaseModel):
    """Single indicator specification."""

    name: str = Field(..., min_length=1, description="Indicator name (e.g. 'ema', 'rsi', 'macd')")
    params: dict[str, int | float] = Field(default_factory=dict, description="Indicator parameters")


class ChartIndicatorPoint(BaseModel):
    t: int
    value: float | None


class ChartIndicatorResult(BaseModel):
    id: str
    panel: str
    type: str
    color: str
    data: list[ChartIndicatorPoint] | dict[str, list[ChartIndicatorPoint]]
    refs: list[float] = Field(default_factory=list)
    default_visible: bool | None = None


class ChartIndicatorBar(BaseModel):
    """One canonical bar supplied by a chart that already owns its data."""

    t: int = Field(..., ge=0, description="Bar-close timestamp as int64 ms UTC")
    o: float = Field(..., allow_inf_nan=False)
    h: float = Field(..., allow_inf_nan=False)
    l: float = Field(..., allow_inf_nan=False)
    c: float = Field(..., allow_inf_nan=False)
    v: float = Field(..., ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_ohlc(self) -> ChartIndicatorBar:
        if self.l > min(self.o, self.c) or self.h < max(self.o, self.c) or self.l > self.h:
            raise ValueError("bar must satisfy low <= open/close <= high")
        return self


class ChartIndicatorBatchRequest(BaseModel):
    """Compute indicators on the caller's exact, already-bucketed bars."""

    symbol: str = Field(..., min_length=1, max_length=16)
    bars: list[ChartIndicatorBar] = Field(..., min_length=1, max_length=20_000)
    indicators: list[ChartIndicatorEntry] = Field(..., min_length=1, max_length=12)


class ChartIndicatorBatchResponse(BaseModel):
    symbol: str
    indicators: list[ChartIndicatorResult]

    @classmethod
    def from_engine_result(
        cls,
        symbol: str,
        indicators: list[dict[str, Any]],
    ) -> ChartIndicatorBatchResponse:
        return cls(
            symbol=symbol,
            indicators=[ChartIndicatorResult.model_validate(item) for item in indicators],
        )


class ChartDataRequest(BaseModel):
    """Request for chart data with resampled bars and indicators."""

    ticker: str = Field(..., min_length=1, max_length=20, description="Ticker symbol")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    timeframe: str = Field("1D", description="Timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 1D, 1W, 1M")
    session: str = Field("rth", description="'rth' for regular trading hours, 'extended' for all hours")
    forward_fill: bool = Field(False, description="Fill missing bars with previous close (volume=0)")
    indicators: list[ChartIndicatorEntry] = Field(
        default_factory=list,
        description="Indicators to compute on resampled bars",
    )
    compute_all_indicators: bool = Field(
        False,
        description="When True, compute all indicators with default params (ignores 'indicators' list)",
    )
    adjusted: bool = Field(True, description="Adjust for splits/dividends (Polygon default: true)")


class AllowedTimeframesRequest(BaseModel):
    """Request for allowed timeframes given a date range."""

    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    session: str = Field("rth", description="'rth' or 'extended'")
