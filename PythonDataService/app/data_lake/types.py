"""Pydantic models for the ensure_data contract.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.1, § 4.2

Spec-update corrections applied (post-plan review):
- ``include_lean_metadata`` field removed; LEAN metadata is an unconditional
  Phase 0 prerequisite, not gated by a flag.
- ``lean_image_digest`` is required (no default); it is the source of the
  LEAN-image-extracted session calendar and is mandatory for every request.
- ``'quote'`` in ``data_types`` requires ``'trade'`` to also be present;
  quote artifacts are derived from same-day trade bytes.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")
# Mirrors the DataLakeArtifacts.Symbol column: character varying(20)
# (Backend/Migrations/20260521033222_AddDataLakeArtifactsAndRuns.cs). Shared
# by DataRunSpec's write-path validator and the coverage endpoint's
# query-param validator (app/routers/data_lake.py) so neither can accept a
# symbol the catalog could never actually store.
MAX_SYMBOL_LENGTH = 20
_MAX_RANGE_YEARS = 5
# Shared by DataRunSpec's write-window validator and the coverage endpoint's
# read-window validator (app/routers/data_lake.py) — one constant, one
# computation (trading_range_span_days below), so the two can't silently
# drift on what counts as "in range" the way they did before (#1835 review).
MAX_TRADING_RANGE_DAYS = _MAX_RANGE_YEARS * 366

# Mirrors the shared DataLakeArtifacts.PriceAdjustmentMode CHECK constraint
# (ck_price_adjustment_mode_enum, Backend/Migrations/20260521033222_...).
# DataRunSpec deliberately narrows to "raw" below — the only mode the v1
# fetch pipeline can produce — rather than accepting the full vocabulary;
# the coverage endpoint (a read over whatever the catalog actually holds)
# uses the full set.
PriceAdjustmentMode = Literal["raw", "polygon_split_adjusted", "lean_adjusted"]

# Mirrors the shared DataLakeArtifacts.Status CHECK constraint
# (ck_status_enum) — Python and the .NET side must not drift on what this
# column can hold. ``"missing"`` is NOT a DB value: the coverage endpoint
# synthesizes it for a calendar session with no matching catalog row.
ArtifactStatus = Literal["fetching", "complete", "stale", "failed"]


def trading_range_span_days(start: date, end: date) -> int:
    """Inclusive day count of a closed ``[start, end]`` trading-date window."""
    return (end - start).days + 1


class DataRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    run_type: Literal["python_lab", "lean_lab"]
    requester: str | None = None
    strategy_execution_id: int | None = None

    market: Literal["usa"] = "usa"
    symbols: list[str] = Field(min_length=1)
    start_trading_date: date
    end_trading_date: date

    resolution: Literal["minute"] = "minute"
    data_types: list[Literal["trade", "quote"]] = ["trade"]
    # Deliberate subset of PriceAdjustmentMode (above) — the only mode the
    # v1 fetch pipeline can produce, not an independent copy of the vocabulary.
    price_adjustment_mode: Literal["raw"] = "raw"
    provider: Literal["polygon"] = "polygon"

    include_factor_files: bool = True
    include_map_files: bool = True
    # Daily-trade (resolution="daily") is a whole-range, per-symbol rollup
    # derived from every minute-trade artifact in [start_trading_date,
    # end_trading_date] — see ensure_data._process_daily_trade_artifact.
    # A caller that ensures the same symbol in successive narrower
    # sub-ranges (e.g. the Task 6 backfill job, one day at a time) must
    # opt out of it per sub-range call: recomputing it from a smaller
    # source set than a prior call collides with the already-completed
    # wider/narrower rollup and reports a spurious data_contract_mismatch
    # (re-aggregation is intentionally out of scope for Slice 1c — see the
    # docstring on _process_daily_trade_artifact). Default True preserves
    # existing single-shot-ensure behavior.
    include_daily_trade: bool = True
    # lean_image_digest is required — source of the LEAN-image-extracted
    # session calendar used by ensure_data's Phase 0 bootstrap.
    lean_image_digest: str

    force_refresh: bool = False
    fetch_timeout_seconds: int = Field(default=600, ge=10, le=7200)

    @model_validator(mode="after")
    def _validate(self) -> DataRunSpec:
        # Symbols: uppercase canonical, within the catalog's storable length.
        for sym in self.symbols:
            if not SYMBOL_RE.match(sym):
                raise ValueError(f"symbol must match {SYMBOL_RE.pattern}: {sym!r}")
            if len(sym) > MAX_SYMBOL_LENGTH:
                raise ValueError(f"symbol exceeds {MAX_SYMBOL_LENGTH}-char catalog limit: {sym!r}")
        # Date ordering.
        if self.start_trading_date > self.end_trading_date:
            raise ValueError(f"start_trading_date {self.start_trading_date} > end_trading_date {self.end_trading_date}")
        # Range cap.
        span_days = trading_range_span_days(self.start_trading_date, self.end_trading_date)
        if span_days > MAX_TRADING_RANGE_DAYS:
            raise ValueError(f"range exceeds {_MAX_RANGE_YEARS}-year cap ({span_days} days requested)")
        # Quote requires trade: quote artifacts are derived from same-day trade
        # bytes; without a source trade artifact, quote synthesis cannot proceed.
        if "quote" in self.data_types and "trade" not in self.data_types:
            raise ValueError(
                "'quote' in data_types requires 'trade' to also be present — "
                "quote artifacts are derived from same-day trade bytes; without "
                "a source trade artifact, quote synthesis cannot proceed."
            )
        return self


class ArtifactIdentity(BaseModel):
    """Internal identity tuple — what the catalog claim key looks like."""

    artifact_kind: Literal["time_series_bars", "factor_file", "map_file", "metadata"]
    market: str | None = None
    symbol: str | None = None
    trading_date: date | None = None
    resolution: Literal["minute", "hour", "daily"] | None = None
    data_type: Literal["trade", "quote"] | None = None
    provider: str
    price_adjustment_mode: str | None = None


class ArtifactRecord(BaseModel):
    id: int
    artifact_kind: str
    market: str | None
    symbol: str | None
    trading_date: date | None
    resolution: str | None
    data_type: str | None
    provider: str
    price_adjustment_mode: str | None
    data_contract_hash: str
    file_path: str
    file_sha256: str
    row_count: int | None
    first_bar_start_ms: int | None
    last_bar_start_ms: int | None


class ArtifactFailure(BaseModel):
    artifact_kind: str
    symbol: str | None
    trading_date: date | None
    data_type: str | None
    reason: Literal[
        "provider_auth_error",
        "provider_entitlement_error",
        "provider_rate_limited",
        "provider_api_error",
        "provider_no_data",
        "unknown_symbol",
        "validation_failed",
        "io_error",
        "lease_timeout",
        "fetch_timeout",
        "unsupported_resolution",
        "unsupported_artifact_kind",
        "corp_action_revision_mismatch",
        "data_contract_mismatch",
        "internal_error",
        # Added for the backfill job (#1836, review round 3):
        "session_not_produced",  # canonical calendar disagrees with ensure_data's own — see app.data_lake.backfill
        "run_aborted",  # a globally-fatal failure stopped the remaining range before it was attempted
    ]
    detail: str | None = None
    provider_status_code: int | None = None
    attempt_count: int = 0


class NonSessionRecord(BaseModel):
    market: str
    trading_date: date
    reason: Literal["weekend", "market_holiday"]


OverallStatus = Literal["complete", "partial", "failed"]


def classify_overall_status(*, has_failures: bool, has_success: bool) -> OverallStatus:
    """Shared complete/partial/failed classification.

    Any success at all downgrades a failure-bearing result from 'failed'
    to 'partial'. Single source of truth: ensure_data.ensure_data and the
    backfill job's day- and whole-range rollups (app.data_lake.backfill)
    all apply this identically rather than each re-deriving the same
    three-way branch.
    """
    if has_failures and has_success:
        return "partial"
    if has_failures:
        return "failed"
    return "complete"


class DataAvailabilityResult(BaseModel):
    request_id: UUID
    overall_status: OverallStatus
    lean_data_root_path: str
    data_availability_hash: str
    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []
    skipped_non_sessions: list[NonSessionRecord] = []
    fetched_artifact_count: int = 0
    reused_artifact_count: int = 0
    refreshed_artifact_count: int = 0
    completed_at_ms: int
    duration_ms: int


# ---------------------------------------------------------------------------
# Task 5: Observatory read-endpoint response models.
#
# Thin projections of the catalog for the future Observatory UI. All
# timestamps are int64 ms UTC; a ``TradingDate`` column value is converted to
# its canonical ET session-open anchor (see temporal-rigor.md) by
# catalog_client (ArtifactDetail, SymbolCoverageSpan) or the router
# (CoverageDay, which merges catalog rows with the calendar's own session
# walk) — not here. These models only describe the wire shape.
# ---------------------------------------------------------------------------


class CoverageDay(BaseModel):
    """One calendar session's artifact status for a symbol/data-type window.

    ``trading_date_ms`` is the session's 09:30 ET open, expressed as
    int64 ms UTC — the canonical anchor for a date-only value. Sessions with
    no matching catalog row report ``status="missing"``; this is never
    emitted for a non-session date, since the router only iterates the
    canonical calendar's sessions in the first place.
    """

    trading_date_ms: int
    status: ArtifactStatus | Literal["missing"]
    artifact_id: int | None = None


class CoverageResponse(BaseModel):
    market: str
    symbol: str
    data_type: str
    resolution: str
    provider: str
    price_adjustment_mode: str
    days: list[CoverageDay] = []


class ArtifactDetail(BaseModel):
    """Full receipt for one catalog row: identity, hashes, and byte metadata."""

    id: int
    artifact_kind: str
    market: str | None
    symbol: str | None
    trading_date_ms: int | None
    resolution: str | None
    data_type: str | None
    provider: str
    provider_params: dict[str, object]
    price_adjustment_mode: str | None
    data_contract_hash: str
    # None (not "") until the artifact reaches Status='complete' and its
    # FileSha256 column is actually populated — an empty string on a
    # fetching/failed row would read as a real hash on a documented receipt.
    content_hash: str | None
    file_path: str
    file_size_bytes: int | None
    status: ArtifactStatus
    row_count: int | None
    first_bar_start_ms: int | None
    last_bar_start_ms: int | None
    fetched_at_ms: int
    completed_at_ms: int | None
    # Diagnostics fail_artifact() persists on a 'failed' row (catalog_client.py).
    # None on a row that has never failed; attempt_count is NOT NULL in the
    # schema (every claim sets it, starting at 1) so it's always present.
    attempt_count: int
    last_error: str | None
    error_message: str | None


class StorageKindTotal(BaseModel):
    artifact_kind: str
    resolution: str | None
    artifact_count: int
    total_bytes: int


class SymbolCoverageSpan(BaseModel):
    symbol: str
    first_trading_date_ms: int | None
    last_trading_date_ms: int | None
    artifact_count: int


class StorageSummaryResponse(BaseModel):
    market: str
    kinds: list[StorageKindTotal] = []
    symbols: list[SymbolCoverageSpan] = []
