"""Shared request and response contracts for chart indicator computation."""

from __future__ import annotations

import math
from numbers import Integral, Real
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.session_anchors import MAX_TIMESTAMP_MS


class ChartIndicatorEntry(BaseModel):
    """Single indicator specification."""

    name: str = Field(..., min_length=1, description="Indicator name (e.g. 'ema', 'rsi', 'macd')")
    params: dict[str, int | float] = Field(default_factory=dict, description="Indicator parameters")

    @field_validator("params", mode="before")
    @classmethod
    def validate_numeric_params(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        for name, parameter in value.items():
            if (
                isinstance(parameter, bool)
                or not isinstance(parameter, Real)
                or (not isinstance(parameter, Integral) and not math.isfinite(parameter))
            ):
                raise ValueError(f"indicator parameter {name} must be a finite number")
        return value


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


class ChartIndicatorSupportResponse(BaseModel):
    names: list[str]


class ChartDataRequest(BaseModel):
    """Request for chart data with resampled bars and indicators.

    Temporal authority (data-lab workspace redesign PRD §12): the numeric
    ``start_ms_utc`` / ``end_ms_utc`` pair is the canonical window form and
    each field takes precedence over its date-string counterpart when
    supplied. Each value resolves by flooring to its UTC calendar date —
    the exact inverse of the frontend's ``utcMsToIsoDate`` — so a numeric
    window and the date string the same request carries never disagree,
    whatever instant inside the day the anchors name (the Data Lab commits
    UTC midnight for the start and the day's final UTC instant for the
    end). Both endpoints of the resolved window are inclusive trading
    dates, matching ``from_date``/``to_date``.
    """

    ticker: str = Field(..., min_length=1, max_length=20, description="Ticker symbol")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    start_ms_utc: int | None = Field(
        None,
        ge=0,
        le=MAX_TIMESTAMP_MS,
        description="Canonical numeric window start (int64 ms UTC); resolves to the UTC calendar "
        "date it falls in, inclusive. Takes precedence over from_date when supplied.",
    )
    end_ms_utc: int | None = Field(
        None,
        ge=0,
        le=MAX_TIMESTAMP_MS,
        description="Canonical numeric window end (int64 ms UTC); resolves to the UTC calendar "
        "date it falls in, inclusive. Takes precedence over to_date when supplied.",
    )
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


class ChartRangePreset(BaseModel):
    """One calendar-resolved quick range ("last N trading sessions").

    Temporal values are int64 ms UTC only — start anchors the first trading
    date at UTC midnight, end anchors the last trading date's final UTC
    instant; clients derive any display strings at the rendering boundary.
    """

    key: str = Field(..., description="Preset key: 1D, 5D, 1M, 3M, 6M, 1Y, 2Y")
    label: str = Field(..., description="Human label for the preset")
    start_ms_utc: int = Field(
        ...,
        ge=0,
        le=MAX_TIMESTAMP_MS,
        description="UTC-midnight anchor of the first trading date (int64 ms UTC)",
    )
    end_ms_utc: int = Field(
        ...,
        ge=0,
        le=MAX_TIMESTAMP_MS,
        description="Final UTC instant of the last trading date, 23:59:59.999 (int64 ms UTC) "
        "— pairs with start_ms_utc as an inclusive trading-date window",
    )
    session_count: int = Field(..., ge=1, description="Scheduled NYSE sessions in the window")
    estimated_bars_per_timeframe: dict[str, int] = Field(
        ...,
        description="Calendar-arithmetic bar estimate per timeframe for this window "
        "(same estimator as /api/chart/allowed-timeframes)",
    )


class ChartRangePresetsResponse(BaseModel):
    """Response for GET /api/chart/range-presets."""

    presets: list[ChartRangePreset]


class AllowedTimeframesRequest(BaseModel):
    """Request for allowed timeframes given a date range."""

    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    session: str = Field("rth", description="'rth' or 'extended'")


class ChartDataBar(BaseModel):
    """One resampled OHLCV bar of the /api/chart/data success payload."""

    t: int = Field(..., ge=0, description="Bar timestamp as int64 ms UTC")
    o: float | None = Field(..., description="Open; null when the source row was absent")
    h: float | None = None
    l: float | None = None
    c: float | None = None
    v: float = Field(default=0.0, ge=0.0)
    session: str | None = Field(default=None, description="Present only on bars the resampler tagged")
    synthetic: bool | None = Field(
        default=None, description="True only on gap-filled synthetic bars"
    )


class ChartGapDetail(BaseModel):
    before_ts: int
    after_ts: int
    duration_minutes: int
    classification: str


class ChartQualityReport(BaseModel):
    """Resample-quality receipt mirroring chart_service.QualityReport."""

    raw_bar_count: int
    duplicates_removed: int
    gaps_found: int
    largest_gap_minutes: int
    missing_sessions: int
    session_coverage_pct: float
    synthetic_bars: int
    resampled_bar_count: int
    gap_details: list[ChartGapDetail] = Field(default_factory=list)
    missing_session_dates: list[str] = Field(default_factory=list)
    flat_bars_detected: int
    ohlc_violations_detected: int
    out_of_order_fixed: int


class ChartDataResponse(BaseModel):
    """Success payload of POST /api/chart/data.

    The route's handler computes a plain dict; declaring it as the
    ``response_model`` publishes the contract (ADR 0031 generated OpenAPI
    types) and pins the bar keys (``t``/``o``/``h``/``l``/``c``/``v``) the
    Data Lab charts already consume, so no client invents a transport
    mirror. Error responses are typed ``detail`` payloads raised as
    HTTPException and are therefore not part of this model.
    """

    bars: list[ChartDataBar]
    indicators: list[ChartIndicatorResult] = Field(default_factory=list)
    quality: ChartQualityReport
    allowed_timeframes: list[str]
    estimated_bars_per_timeframe: dict[str, int]
    recommended_timeframe: str
    meta: dict[str, bool]
    bar_sources: dict[str, Any] | None = Field(
        default=None,
        description="Per-source ingest receipts; present only when the lake is in the read path",
    )
