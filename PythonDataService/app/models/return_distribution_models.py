"""Request/response models for the return-distribution study.

Wire conventions: snake_case fields; every temporal value is ``int64 ms
UTC`` (window boundaries resolve to inclusive UTC calendar dates inside
Python, through the same authority the chart endpoint uses — there is no
date-string type on this wire); percentages are simple returns in percent
units, never fractions; ``None`` means "this value is genuinely undefined"
(missing segment, or a statistic whose estimator needs a larger sample),
never zero.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.return_distribution import MAX_BINS_PER_SIDE, RETURN_KINDS
from app.utils.session_anchors import MAX_TIMESTAMP_MS

CaptureStatus = Literal["not_attempted", "skipped", "complete", "partial", "failed"]


class ReturnDistributionRequest(BaseModel):
    """One study request: minute bars for ``symbol`` over a numeric window.

    Deliberately standalone rather than a ``TickerRequest`` subclass: the
    study always reads 1-minute extended-session bars from the lake, so the
    sampling block (``timespan``/``multiplier``/``session``) of the bar
    family does not apply and accepting it would promise a knob that does
    not exist. The window travels as int64 ms UTC instants and resolves to
    inclusive UTC calendar dates inside Python (the Data Lab window
    convention: start anchors the UTC midnight of the first trading date,
    end the final instant of the last — ``resolve_request_dates`` is the
    shared conversion authority).
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=20)
    from_ms_utc: int = Field(..., ge=0, le=MAX_TIMESTAMP_MS)
    to_ms_utc: int = Field(..., ge=0, le=MAX_TIMESTAMP_MS)
    bin_width_pct: float = Field(default=0.5, gt=0.0, le=2.0)
    span_pct: float = Field(default=5.0, gt=0.0, le=20.0)

    @model_validator(mode="after")
    def _validate_window(self) -> ReturnDistributionRequest:
        if self.to_ms_utc < self.from_ms_utc:
            raise ValueError(f"to_ms_utc ({self.to_ms_utc}) must be >= from_ms_utc ({self.from_ms_utc})")
        return self

    @model_validator(mode="after")
    def _validate_bin_count(self) -> ReturnDistributionRequest:
        # Bounds the allocated edge list (memory) and the rendered bars; the
        # integer-multiple rule itself is the study's to report.
        ratio = self.span_pct / self.bin_width_pct
        if ratio > MAX_BINS_PER_SIDE:
            raise ValueError(
                f"span_pct / bin_width_pct = {ratio:g} exceeds the maximum of "
                f"{MAX_BINS_PER_SIDE} bins per side (at most {2 * MAX_BINS_PER_SIDE + 1} bins total)"
            )
        return self


class CoverageInfo(BaseModel):
    requested_sessions: int
    returned_sessions: int
    missing_sessions: int
    excluded_sessions: int
    first_session_open_ms_utc: int | None
    last_session_open_ms_utc: int | None


class ExtremeDayModel(BaseModel):
    session_open_ms_utc: int
    value_pct: float


class DistributionStatsModel(BaseModel):
    """``None`` stats are undefined for the sample (n too small, or zero
    variance for the standardized moments) — not zero."""

    n_days: int
    mean_pct: float
    std_pct: float | None
    annualized_vol_pct: float | None
    skewness: float | None
    excess_kurtosis: float | None
    var_95_pct: float
    cvar_95_pct: float
    best_day: ExtremeDayModel
    worst_day: ExtremeDayModel


class HistogramBinModel(BaseModel):
    lower_pct: float | None
    upper_pct: float | None
    count: int
    is_edge: bool


class KindDistributionModel(BaseModel):
    kind: Literal["close_to_close", "session", "overnight"]
    bins: list[HistogramBinModel]
    normal_expected_counts: list[float]
    stats: DistributionStatsModel


class DayReturnsModel(BaseModel):
    """One day's returns plus ``bin_indices``: this day's histogram bin per
    return kind (index into that kind's ``bins`` list, edge bins included),
    stamped by the same membership function the counts were tallied with.
    Consumers select a basket's days by identity — they never re-derive
    membership. ``None`` when that kind's value is undefined for the day."""

    session_open_ms_utc: int
    close_to_close_pct: float | None
    session_pct: float | None
    overnight_pct: float | None
    pre_market_pct: float | None
    morning_pct: float | None
    afternoon_pct: float | None
    after_hours_pct: float | None
    volume: int
    bin_indices: dict[str, int | None]


class CaptureReceiptModel(BaseModel):
    """What the on-demand lake capture did for this request.

    ``status`` is the closed contract of
    ``app.services.return_distribution_service.CaptureStatus``; clients
    must render every state (the generated union type enforces it).
    """

    status: CaptureStatus
    fetched_artifact_count: int
    detail: str | None = None


class ReturnDistributionMeta(BaseModel):
    symbol: str
    from_ms_utc: int
    to_ms_utc: int
    resolution: Literal["1m"] = "1m"
    bin_width_pct: float
    span_pct: float
    adjustment: Literal["split_and_dividend", "raw"]
    capture: CaptureReceiptModel | None = None
    warnings: list[str] = Field(default_factory=list)


class ReturnDistributionResponse(BaseModel):
    meta: ReturnDistributionMeta
    coverage: CoverageInfo
    kinds: list[KindDistributionModel]
    days: list[DayReturnsModel]

    @classmethod
    def from_engine_result(
        cls,
        *,
        meta: ReturnDistributionMeta,
        coverage: CoverageInfo,
        result,
    ) -> ReturnDistributionResponse:
        """Build the wire model from the pure module's dataclass result.

        Pure field-by-field mapping — no arithmetic happens here (Python
        owns the math; models are transport).
        """
        return cls(
            meta=meta,
            coverage=coverage,
            kinds=[
                KindDistributionModel(
                    kind=k.kind,
                    bins=[
                        HistogramBinModel(
                            lower_pct=b.lower_pct,
                            upper_pct=b.upper_pct,
                            count=b.count,
                            is_edge=b.is_edge,
                        )
                        for b in k.histogram.bins
                    ],
                    normal_expected_counts=list(k.normal_expected_counts),
                    stats=DistributionStatsModel(
                        n_days=k.stats.n_days,
                        mean_pct=k.stats.mean_pct,
                        std_pct=k.stats.std_pct,
                        annualized_vol_pct=k.stats.annualized_vol_pct,
                        skewness=k.stats.skewness,
                        excess_kurtosis=k.stats.excess_kurtosis,
                        var_95_pct=k.stats.var_95_pct,
                        cvar_95_pct=k.stats.cvar_95_pct,
                        best_day=ExtremeDayModel(
                            session_open_ms_utc=k.stats.best_day.session_open_ms_utc,
                            value_pct=k.stats.best_day.value_pct,
                        ),
                        worst_day=ExtremeDayModel(
                            session_open_ms_utc=k.stats.worst_day.session_open_ms_utc,
                            value_pct=k.stats.worst_day.value_pct,
                        ),
                    ),
                )
                for k in result.kinds
            ],
            days=[
                DayReturnsModel(
                    session_open_ms_utc=d.session_open_ms_utc,
                    close_to_close_pct=d.close_to_close_pct,
                    session_pct=d.session_pct,
                    overnight_pct=d.overnight_pct,
                    pre_market_pct=d.pre_market_pct,
                    morning_pct=d.morning_pct,
                    afternoon_pct=d.afternoon_pct,
                    after_hours_pct=d.after_hours_pct,
                    volume=d.volume,
                    bin_indices=dict(zip(RETURN_KINDS, d.bin_indices, strict=True)),
                )
                for d in result.days
            ],
        )


class NotCapturedDetail(BaseModel):
    """The typed 404 body's ``detail`` (minus the extra_forbidden quirks)."""

    error_code: Literal["NOT_CAPTURED"] = "NOT_CAPTURED"
    message: str
    capture_note: str | None = None
    captured_symbols: list[str] = Field(default_factory=list)


class ReturnDistributionNotCapturedResponse(BaseModel):
    """404 body for a symbol the lake cannot address or capture could not fill."""

    detail: NotCapturedDetail


class InsufficientCoverageDetail(BaseModel):
    """The typed 400 body's ``detail`` for a too-thin usable sample."""

    error_code: Literal["INSUFFICIENT_COVERAGE"] = "INSUFFICIENT_COVERAGE"
    message: str
    requested_sessions: int
    available_sessions: int


class ReturnDistributionInsufficientCoverageResponse(BaseModel):
    detail: InsufficientCoverageDetail


class DayCandlesRequest(BaseModel):
    """One drill-down day: extended-session minute candles for a session that
    a study response already named (``session_open_ms_utc`` is the day's
    session-open anchor, so no date string travels)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=20)
    session_open_ms_utc: int = Field(..., ge=0, le=MAX_TIMESTAMP_MS)


class DayCandleBarModel(BaseModel):
    t: int = Field(..., ge=0, description="Bar start as int64 ms UTC")
    o: float
    h: float
    l: float
    c: float
    v: float = Field(default=0.0, ge=0.0)


class DayCandlesResponse(BaseModel):
    """Minute candles on the study's own price basis.

    Read from the same raw lake root and scaled by the same LEAN
    factor-file multiplier the study applied to that day's anchors, so the
    candle pane cannot disagree with the return being inspected (the
    provider-adjusted chart feed applies split-only adjustment)."""

    symbol: str
    session_open_ms_utc: int
    adjustment: Literal["split_and_dividend", "raw"]
    bars: list[DayCandleBarModel]


class DayNotCapturedDetail(BaseModel):
    error_code: Literal["NOT_CAPTURED", "DAY_NOT_CAPTURED"]
    message: str
    trading_date: str | None = None


class DayCandlesNotCapturedResponse(BaseModel):
    detail: DayNotCapturedDetail
