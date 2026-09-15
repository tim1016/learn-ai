"""Request/response models for the return-distribution study.

Wire conventions: snake_case fields; every temporal value is ``int64 ms
UTC`` anchored at the session's scheduled open (the lake's trading-date
wire convention); percentages are simple returns in percent units, never
fractions; ``None`` means "this segment genuinely produced no data", not
zero.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.ticker_request import DATE_PATTERN


class ReturnDistributionRequest(BaseModel):
    """One study request: minute bars for ``symbol`` over a calendar window.

    Deliberately standalone rather than a ``TickerRequest`` subclass: the
    study always reads 1-minute extended-session bars from the lake, so the
    sampling block (``timespan``/``multiplier``/``session``) of the bar
    family does not apply and accepting it would promise a knob that does
    not exist.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., pattern=DATE_PATTERN)
    to_date: str = Field(..., pattern=DATE_PATTERN)
    bin_width_pct: float = Field(default=0.5, gt=0.0, le=2.0)
    span_pct: float = Field(default=5.0, gt=0.0, le=20.0)

    @model_validator(mode="after")
    def _validate_dates(self) -> ReturnDistributionRequest:
        try:
            f = Date.fromisoformat(self.from_date)
            t = Date.fromisoformat(self.to_date)
        except ValueError as e:
            raise ValueError(f"invalid calendar date: {e}") from e
        if t < f:
            raise ValueError(f"to_date ({self.to_date}) must be >= from_date ({self.from_date})")
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
    n_days: int
    mean_pct: float
    std_pct: float
    annualized_vol_pct: float
    skewness: float
    excess_kurtosis: float
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
    session_open_ms_utc: int
    close_to_close_pct: float | None
    session_pct: float | None
    overnight_pct: float | None
    pre_market_pct: float | None
    morning_pct: float | None
    afternoon_pct: float | None
    after_hours_pct: float | None
    volume: int


class CaptureReceiptModel(BaseModel):
    """What the on-demand lake capture did for this request."""

    attempted: bool
    status: str
    fetched_artifact_count: int
    detail: str | None = None


class ReturnDistributionMeta(BaseModel):
    symbol: str
    from_date: str
    to_date: str
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
                )
                for d in result.days
            ],
        )
