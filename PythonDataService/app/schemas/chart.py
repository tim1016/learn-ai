"""Shared request and response contracts for chart indicator computation."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
