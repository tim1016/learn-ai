"""Pydantic models for Indicator Reliability API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IndicatorParam(BaseModel):
    """A single indicator parameter configuration."""

    name: str
    value: int | float


class IndicatorReliabilityRequest(BaseModel):
    """Request to analyze indicator reliability."""

    ticker: str = Field(..., description="Stock ticker symbol")
    indicator_name: str = Field(..., description="pandas-ta indicator name (e.g., 'rsi', 'ema')")
    indicator_params: dict[str, int | float] = Field(
        default_factory=dict,
        description="Indicator parameters (e.g., {'length': 14})",
    )
    start_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    end_date: str = Field(..., description="End date (YYYY-MM-DD)")
    horizons: list[int] = Field(
        default=[1, 5, 10, 15, 30],
        description="Forward horizons (in bars) to analyze",
    )
    include_slope: bool = Field(
        default=False,
        description="Also compute IC on indicator slope (1-bar change)",
    )
    timespan: str = Field(default="minute", description="Bar timespan")
    multiplier: int = Field(default=1, description="Bar multiplier")


class HorizonICResult(BaseModel):
    """IC analysis result for a single forward horizon."""

    horizon: int = Field(..., description="Forward horizon in bars")
    mean_ic: float = Field(..., description="Mean Information Coefficient")
    t_stat: float = Field(..., description="Standard t-statistic")
    p_value: float = Field(..., description="Standard p-value")
    nw_t_stat: float | None = Field(None, description="Newey-West corrected t-stat")
    nw_p_value: float | None = Field(None, description="Newey-West corrected p-value")
    effective_n: int = Field(..., description="Effective sample size")
    interpretation: str = Field(..., description="Human-readable verdict")


class IndicatorReliabilityResponse(BaseModel):
    """Response from indicator reliability analysis."""

    success: bool
    ticker: str
    indicator_name: str
    indicator_params: dict[str, int | float]
    display_name: str = Field(..., description="Formatted display name (e.g., 'RSI (14)')")
    category: str | None = Field(None, description="Indicator category (e.g., 'momentum')")
    start_date: str
    end_date: str
    bar_count: int = Field(..., description="Number of bars analyzed")

    # Results per horizon
    results: list[HorizonICResult] = Field(
        default_factory=list,
        description="IC results for each forward horizon",
    )

    # Optional slope IC results
    slope_results: list[HorizonICResult] | None = Field(
        None,
        description="IC results for indicator slope (if include_slope=True)",
    )

    # Daily IC series for charting (using best horizon)
    daily_ic_values: list[float] = Field(default_factory=list)
    daily_ic_dates: list[str] = Field(default_factory=list)
    best_horizon: int | None = Field(None, description="Horizon with strongest signal")

    error: str | None = None


class IndicatorInfo(BaseModel):
    """Metadata about an available indicator."""

    name: str
    category: str
    description: str
    params: list[dict] = Field(default_factory=list)
