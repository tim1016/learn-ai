"""Response schema for ``POST /api/dataset/plan`` (data-lab workspace redesign §12).

Every numeric or enumerable claim a Data Lab receipt renders is authored
here in Python — Angular renders it unchanged. Bar counts are estimates
and are typed as such: assumptions and provenance ship alongside.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DatasetPlanResponse(BaseModel):
    """Fetch-free planning receipt for a dataset generation recipe."""

    ticker: str = Field(..., description="Ticker the plan was resolved for")
    window_start_ms_utc: int = Field(
        ...,
        description="Resolved half-open window start (int64 ms UTC). Date-only intent resolves to the session open.",
    )
    window_end_ms_utc: int = Field(
        ...,
        description="Resolved EXCLUSIVE window end (int64 ms UTC). Date-only intent resolves to the next session open.",
    )
    exchange_sessions: list[str] = Field(
        ...,
        description="Scheduled exchange session dates inside the requested range (YYYY-MM-DD), ascending.",
    )
    exchange_session_opens_ms_utc: list[int] = Field(
        ...,
        description="Calendar-derived session-open anchor for each entry of exchange_sessions, "
        "in the same order (int64 ms UTC). The canonical wire form of the session list.",
    )
    session_count: int = Field(..., ge=0, description="Number of scheduled exchange sessions in range")
    output_columns: list[str] = Field(
        ...,
        description="Canonical ordered output column list, projected by the same function the ZIP path uses.",
    )
    output_column_count: int = Field(..., ge=0, description="len(output_columns)")
    estimated_bars: int = Field(
        ...,
        ge=0,
        description="ESTIMATE only — arithmetic from session count and timeframe. No bars were fetched.",
    )
    estimate_assumptions: list[str] = Field(
        ...,
        description="Explicit assumptions behind estimated_bars; the estimate is only meaningful with these.",
    )
    estimate_provenance: str = Field(
        ...,
        description="Where the estimate's inputs came from (calendar source and method).",
    )
    companion_dependencies: list[str] = Field(
        ...,
        description="Companion data sources the generation run would fetch (names, not payloads).",
    )
    warnings: list[str] = Field(..., description="Human-readable cautions (tick-level volume, options workload, …).")
    allowed_timeframes: list[str] = Field(
        ...,
        description="Chart timeframes whose estimated bar count stays under the chart bar budget.",
    )
    recommended_timeframes: list[str] = Field(
        ...,
        description="Timeframe(s) recommended for interactive charting over this range.",
    )
    exchange: str = Field(..., description="Exchange the calendar resolves against (e.g. NYSE).")
    calendar_timezone: str = Field(..., description="Exchange calendar timezone (IANA name).")
    calendar_version: str = Field(..., description="Version of the underlying calendar library.")
