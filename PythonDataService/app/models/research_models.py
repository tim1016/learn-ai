"""Pydantic v2 models for research endpoint request/response."""
from __future__ import annotations

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    """Single OHLCV bar."""

    timestamp: int = Field(..., description="Milliseconds since epoch")
    open: float
    high: float
    low: float
    close: float
    volume: float


class RunFeatureResearchRequest(BaseModel):
    """Request body for POST /research/run-feature."""

    ticker: str = Field(..., description="Stock symbol (e.g. AAPL)")
    feature_name: str = Field(..., description="Feature to validate (e.g. momentum_5m)")
    bars: list[OHLCVBar] = Field(..., description="OHLCV bars (1-minute)")
    start_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    end_date: str = Field(..., description="ISO date (YYYY-MM-DD)")


class QuantileBinResponse(BaseModel):
    """Single quantile bin in the response."""

    bin_number: int
    lower_bound: float
    upper_bound: float
    mean_return: float
    count: int


class RunFeatureResearchResponse(BaseModel):
    """Response body for POST /research/run-feature."""

    success: bool
    ticker: str
    feature_name: str
    start_date: str
    end_date: str
    bars_used: int
    mean_ic: float
    ic_t_stat: float
    ic_p_value: float
    adf_pvalue: float
    kpss_pvalue: float
    is_stationary: bool
    passed_validation: bool
    quantile_bins: list[QuantileBinResponse] = []
    is_monotonic: bool = False
    monotonicity_ratio: float = 0.0
    ic_values: list[float] = []
    ic_dates: list[str] = []
    error: str | None = None


class FeatureInfoResponse(BaseModel):
    """Feature metadata for the information panel."""

    name: str
    formula_latex: str
    variables: str
    example: str
    interpretation: str
    implementation_note: str
    window: int
    category: str
