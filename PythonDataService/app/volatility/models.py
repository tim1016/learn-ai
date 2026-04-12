"""
Pydantic Models for the Implied Volatility Surface API
=======================================================

Request / response schemas for the ``/api/volatility`` endpoints.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ── Enums ────────────────────────────────────────────────────────────────────

class SurfaceMethodEnum(str, Enum):
    VARIANCE = "variance"
    SABR = "sabr"
    SVI = "svi"


class OptionTypeEnum(str, Enum):
    CALL = "call"
    PUT = "put"


# ── Input models ─────────────────────────────────────────────────────────────

class OptionRecord(BaseModel):
    """A single option contract with market data."""

    strike: float = Field(..., gt=0, description="Strike price")
    ttm: float = Field(..., gt=0, description="Time to maturity in years")
    option_price: float = Field(..., gt=0, description="Mid market price")
    is_call: bool = Field(..., description="True for call, False for put")
    bid: Optional[float] = Field(None, ge=0, description="Bid price")
    ask: Optional[float] = Field(None, ge=0, description="Ask price")
    open_interest: Optional[int] = Field(None, ge=0)
    volume: Optional[int] = Field(None, ge=0)

    @field_validator("ask")
    @classmethod
    def ask_gte_bid(cls, v: Optional[float], info) -> Optional[float]:
        bid = info.data.get("bid")
        if v is not None and bid is not None and v < bid:
            raise ValueError(f"ask ({v}) must be >= bid ({bid})")
        return v


class SurfaceBuildRequest(BaseModel):
    """Request to build an implied volatility surface."""

    ticker: str = Field(..., min_length=1, description="Underlying ticker symbol")
    spot: float = Field(..., gt=0, description="Current underlying price")
    rate: float = Field(0.05, description="Risk-free rate (continuous)")
    dividend: float = Field(0.0, ge=0, description="Dividend yield (continuous)")
    eval_date: str = Field("", description="Evaluation date (YYYY-MM-DD)")
    method: SurfaceMethodEnum = Field(
        SurfaceMethodEnum.VARIANCE,
        description="Surface fitting method",
    )
    options: list[OptionRecord] = Field(
        ..., min_length=1, description="Option chain records"
    )
    min_contracts_per_slice: int = Field(
        5, ge=2, description="Min contracts per expiry to include"
    )
    sabr_beta: float = Field(
        0.5, ge=0, le=1, description="SABR beta (fixed parameter)"
    )
    build_bid_ask: bool = Field(
        False, description="Build separate bid/ask surfaces"
    )


class VolQuery(BaseModel):
    """Query a built surface at specific points."""

    strike: float = Field(..., gt=0)
    ttm: float = Field(..., gt=0)


class SurfaceQueryRequest(BaseModel):
    """Request to query vol at specific points."""

    queries: list[VolQuery] = Field(..., min_length=1)


class SurfaceGridRequest(BaseModel):
    """Request to evaluate the surface on a regular grid."""

    strike_min: float = Field(..., gt=0)
    strike_max: float = Field(..., gt=0)
    n_strikes: int = Field(50, ge=10, le=500)
    ttm_list: Optional[list[float]] = Field(
        None, description="Specific TTMs to evaluate; defaults to fitted expiries"
    )


# ── Output models ────────────────────────────────────────────────────────────

class SliceDiagnosticsResponse(BaseModel):
    """Diagnostics for a single expiry slice."""

    ttm: float
    n_contracts: int
    n_solved: int
    n_failed: int
    fit_method: str
    fit_rmse: float
    butterfly_violations: int = 0
    arbitrage_passed: bool = True


class FitParamsResponse(BaseModel):
    """Fitted parameters for a single expiry slice."""

    ttm: float
    method: str
    params: dict[str, float]
    rmse: float


class SurfaceDiagnosticsResponse(BaseModel):
    """Aggregate diagnostics for the full surface."""

    n_expiries: int
    n_total_contracts: int
    n_total_solved: int
    n_total_failed: int
    method: str
    slices: list[SliceDiagnosticsResponse]
    warnings: list[str]
    valid: bool


class SurfaceBuildResponse(BaseModel):
    """Response from building a vol surface."""

    success: bool
    ticker: str
    method: str
    spot: float
    n_expiries: int
    fitted_params: list[FitParamsResponse]
    diagnostics: SurfaceDiagnosticsResponse
    surface_id: str = Field(
        "", description="Opaque ID for querying this surface later"
    )


class VolQueryResponse(BaseModel):
    """Response for a single vol query."""

    strike: float
    ttm: float
    iv: Optional[float]
    error: Optional[str] = None


class SurfaceQueryResponse(BaseModel):
    """Response for batch vol queries."""

    results: list[VolQueryResponse]


class GridPointResponse(BaseModel):
    """A single grid point."""

    strike: float
    ttm: float
    iv: Optional[float]


class SurfaceGridResponse(BaseModel):
    """Response for grid evaluation."""

    points: list[GridPointResponse]
    n_strikes: int
    n_expiries: int


# ── Conventions and Filters models ───────────────────────────────────────────

class ConventionsModel(BaseModel):
    """Market conventions for surface building."""

    rate: float = Field(0.05, description="Risk-free rate (continuous)")
    dividend_yield: float = Field(
        0.0, ge=0, description="Dividend yield (continuous)"
    )
    day_count: str = Field("Actual365Fixed", description="Day count convention")
    forward_model: str = Field("bsm", description="Forward model (bsm, simple)")


class DataFiltersModel(BaseModel):
    """Data quality filters for option contracts."""

    min_dte: int = Field(7, ge=1, description="Minimum days to expiry")
    max_dte: int = Field(365, le=1825, description="Maximum days to expiry")
    min_open_interest: int = Field(10, ge=0, description="Minimum open interest")
    max_spread_pct: float = Field(
        0.20, ge=0, le=1, description="Max bid-ask spread as % of mid"
    )


# ── Build request models ─────────────────────────────────────────────────────

class BuildFromTickerRequest(BaseModel):
    """Request to build IV surface from ticker and date."""

    ticker: str = Field(..., min_length=1, description="Underlying ticker symbol")
    date: str = Field(..., description="Evaluation date (YYYY-MM-DD)")
    method: SurfaceMethodEnum = Field(
        SurfaceMethodEnum.SVI, description="Surface fitting method"
    )
    conventions: ConventionsModel = Field(
        default_factory=ConventionsModel, description="Market conventions"
    )
    filters: DataFiltersModel = Field(
        default_factory=DataFiltersModel, description="Data quality filters"
    )


class BuildFromCsvRequest(BaseModel):
    """Request to build IV surface from raw CSV data."""

    csv_content: str = Field(..., description="Raw CSV text with option records")
    ticker: str = Field("", description="Optional ticker symbol for metadata")
    spot: float = Field(..., gt=0, description="Current underlying price")
    method: SurfaceMethodEnum = Field(
        SurfaceMethodEnum.SVI, description="Surface fitting method"
    )
    conventions: ConventionsModel = Field(
        default_factory=ConventionsModel, description="Market conventions"
    )


# ── Build response models ────────────────────────────────────────────────────

class SurfaceBuildSummary(BaseModel):
    """Lightweight summary of a built surface."""

    surface_id: str = Field(..., description="Opaque surface identifier")
    ticker: str = Field(..., description="Underlying ticker")
    spot: float = Field(..., gt=0, description="Spot price at build time")
    method: str = Field(..., description="Fitting method used")
    date: str = Field(..., description="Build date (YYYY-MM-DD)")
    cached: bool = Field(False, description="Whether surface was loaded from cache")
    n_expiries: int = Field(..., ge=0, description="Number of expiry slices")
    n_contracts_accepted: int = Field(
        ..., ge=0, description="Number of contracts accepted"
    )
    n_contracts_rejected: int = Field(..., ge=0, description="Number rejected")
    build_time_ms: int = Field(..., ge=0, description="Build time in milliseconds")
    health_score: int = Field(
        ..., ge=0, le=100, description="Health score 0-100"
    )
    valid: bool = Field(..., description="Whether surface is valid/usable")
    schema_version: str = Field("1.0", description="Schema version")


# ── Grid response models ─────────────────────────────────────────────────────

class GridMetaModel(BaseModel):
    """Metadata for a matrix grid response."""

    spot: float = Field(..., gt=0, description="Spot price")
    forwards: list[float] = Field(..., description="Forward prices per expiry")
    n_strikes: int = Field(..., ge=0, description="Number of strikes")
    n_expiries: int = Field(..., ge=0, description="Number of expiries")
    expiry_dates: list[str] = Field(..., description="Expiry dates (YYYY-MM-DD)")


class MatrixGridResponse(BaseModel):
    """Matrix grid of IV surface values."""

    x: list[float] = Field(..., description="Moneyness axis values")
    y: list[int] = Field(..., description="DTE days axis")
    z: list[list[Optional[float]]] = Field(
        ..., description="IV matrix [n_expiries × n_strikes]"
    )
    x_label: str = Field(
        ..., description="X-axis label (log_moneyness, moneyness, strike)"
    )
    y_label: str = Field("dte_days", description="Y-axis label")
    z_label: str = Field("implied_vol", description="Z-axis label")
    meta: GridMetaModel = Field(..., description="Grid metadata")


class MatrixGridRequest(BaseModel):
    """Request to retrieve a matrix grid of IV values."""

    axis: str = Field(
        "log_moneyness",
        description="X-axis convention (log_moneyness, moneyness, strike)",
    )
    n_strikes: int = Field(50, ge=10, le=500, description="Number of strikes")
    dte_days: Optional[list[int]] = Field(
        None, description="Specific DTE days to include"
    )
    expiry_dates: Optional[list[str]] = Field(
        None, description="Specific expiry dates to include"
    )


# ── Smile response models ────────────────────────────────────────────────────

class SmilePointModel(BaseModel):
    """A single point on a fitted smile curve."""

    x: float = Field(..., description="Moneyness value (K/S or log-moneyness)")
    iv: float = Field(..., ge=0, description="Implied volatility")


class MarketPointModel(BaseModel):
    """A single market quote point."""

    x: float = Field(..., description="Moneyness value")
    iv: float = Field(..., ge=0, description="Implied volatility")
    status: str = Field(..., description="Solver status (solved, invalid, etc.)")


class SmileSliceResponse(BaseModel):
    """A single expiry slice with fitted and market points."""

    ttm: float = Field(..., gt=0, description="Time to maturity in years")
    dte_days: int = Field(..., ge=0, description="Days to expiry")
    expiry_date: str = Field(..., description="Expiry date (YYYY-MM-DD)")
    forward: float = Field(..., gt=0, description="Forward price")
    fitted: list[SmilePointModel] = Field(
        ..., description="Fitted smile points"
    )
    market: list[MarketPointModel] = Field(..., description="Market quotes")


class SmilesResponse(BaseModel):
    """Collection of smile slices."""

    x_label: str = Field(
        ..., description="X-axis label (log_moneyness, moneyness, strike)"
    )
    slices: list[SmileSliceResponse] = Field(..., description="Smile slices")


# ── Diagnostics response models ──────────────────────────────────────────────

class RejectionBreakdown(BaseModel):
    """Breakdown of contract rejections."""

    total_quotes: int = Field(..., ge=0, description="Total quotes received")
    accepted: int = Field(..., ge=0, description="Number accepted")
    rejected: int = Field(..., ge=0, description="Number rejected")
    by_reason: dict[str, int] = Field(
        ..., description="Rejection counts by reason"
    )


class ArbitrageDetail(BaseModel):
    """Arbitrage violation diagnostics."""

    calendar_violations: int = Field(
        0, ge=0, description="Number of calendar spread violations"
    )
    butterfly_violations: int = Field(
        0, ge=0, description="Number of butterfly violations"
    )
    severity: str = Field(
        ...,
        description="Violation severity (none, low, moderate, high)",
    )
    worst_slices: list[dict] = Field(
        default_factory=list, description="Details of worst violations"
    )


class DiagnosticsResponse(BaseModel):
    """Full diagnostics for a built surface."""

    summary: SurfaceBuildSummary = Field(..., description="Build summary")
    rejections: RejectionBreakdown = Field(..., description="Rejection analysis")
    arbitrage: ArbitrageDetail = Field(..., description="Arbitrage analysis")
    fitted_params: list[FitParamsResponse] = Field(
        ..., description="Fitted parameters per slice"
    )
    slices: list[SliceDiagnosticsResponse] = Field(
        ..., description="Per-slice diagnostics"
    )
    health_score: int = Field(
        ..., ge=0, le=100, description="Overall health score 0-100"
    )
    warnings: list[str] = Field(
        default_factory=list, description="List of warnings"
    )


# ── Batch summary models ─────────────────────────────────────────────────────

class DailySummary(BaseModel):
    """Summary of a single day's IV surface."""

    date: str = Field(..., description="Date (YYYY-MM-DD)")
    surface_id: str = Field(..., description="Surface identifier")
    atm_iv: Optional[float] = Field(None, ge=0, description="ATM implied vol")
    rr_25d: Optional[float] = Field(
        None, description="Risk reversal 25-delta"
    )
    bf_25d: Optional[float] = Field(None, description="Butterfly 25-delta")
    skew_slope: Optional[float] = Field(None, description="Vol skew slope")
    n_contracts: int = Field(..., ge=0, description="Contracts used")
    health_score: int = Field(
        ..., ge=0, le=100, description="Health score 0-100"
    )
    cached: bool = Field(False, description="Loaded from cache")


class BatchSummaryRequest(BaseModel):
    """Request to build surfaces for a date range."""

    ticker: str = Field(..., min_length=1, description="Underlying ticker")
    start_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    end_date: str = Field(..., description="End date (YYYY-MM-DD)")
    method: SurfaceMethodEnum = Field(
        SurfaceMethodEnum.SVI, description="Surface fitting method"
    )
    conventions: ConventionsModel = Field(
        default_factory=ConventionsModel, description="Market conventions"
    )
    filters: DataFiltersModel = Field(
        default_factory=DataFiltersModel, description="Data quality filters"
    )
    mode: str = Field(
        "cached",
        description="Build mode (auto, build, cached)",
    )


class BatchSummaryResponse(BaseModel):
    """Response with daily summaries for a date range."""

    ticker: str = Field(..., description="Underlying ticker")
    daily_summaries: list[DailySummary] = Field(
        ..., description="Per-day surface summaries"
    )
