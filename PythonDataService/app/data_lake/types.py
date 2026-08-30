"""Pydantic models for the ensure_data contract.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.1, § 4.2

Spec-update corrections applied (post-plan review):
- ``include_lean_metadata`` field removed; LEAN metadata is an unconditional
  Phase 0 prerequisite, not gated by a flag.
- ``lean_image_digest`` is required (no default); it is the source of the
  LEAN-image-extracted session calendar and is mandatory for every request.
- ``'quote'`` in ``data_types`` requires ``'trade'`` to also be present;
  quote artifacts are derived from same-day trade bytes.
- (#1877) ``start_trading_date``/``end_trading_date`` are no longer wire
  fields; the wire carries ``start_trading_date_ms``/``end_trading_date_ms``
  (int64 ms UTC, anchored at ``CALENDAR_ANCHOR_UTC_HOUR``:00:00.000 UTC) and
  the ``date`` values are exposed as read-only properties derived from them.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import active_root_id
from app.utils.timestamps import ny_datetime

# Every ``data_root_id`` field below defaults to active_root_id() — the
# service's configured active root — so every existing ``ArtifactIdentity``/
# ``ArtifactRecord`` construction site in the codebase keeps working
# unchanged (issue #1876's "current single-root behavior is unchanged end to
# end" acceptance criterion). ``active_root_id`` lives in app.config, not
# app.data_lake.root_identity, specifically so this module-level import here
# cannot cycle back through it — see that module's own comment.

#: The lake's symbol policy. ``DataRunSpec`` enforces it on every write, so it
#: is also the answer to "could the lake ever hold this symbol?" — readers that
#: classify a symbol's provenance must consult it (is_lake_addressable_symbol
#: below) rather than assume any ticker the filesystem tolerates is one
#: ensure_data can seed.
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


def polygon_mode_for(adjusted: bool) -> Literal["raw", "polygon_split_adjusted"]:
    """The lake adjustment mode a Polygon run's ``adjusted`` flag selects.

    One canonical answer to "which mode does this boolean mean". Five call
    sites had each spelled the conditional out inline and #1839 was about to
    add a sixth, so the mapping lives here beside the vocabulary it maps into.

    ``lean_adjusted`` is unreachable from a boolean by construction: it would
    be derived from raw bars plus factor files, and no such producer exists.
    """
    return "polygon_split_adjusted" if adjusted else "raw"


def trading_range_span_days(start: date, end: date) -> int:
    """Inclusive day count of a closed ``[start, end]`` trading-date window."""
    return (end - start).days + 1


def trading_date_at_ms(trading_date_ms: int) -> date:
    """Read a trading date back out of its ``int64 ms UTC`` anchor.

    The inverse of ``trading_calendar.session_open_ms_utc``, and the lake's
    only one. A trading date travels the wire as a single ``int64 ms UTC``
    value anchored at that session's open (``.claude/rules/temporal-rigor.md``,
    "Date-anchored and wall-clock values"); this resolves it back in
    ``America/New_York``, which is what stops the date drifting a calendar day
    for a caller west of UTC.

    Deliberately not fussy about *which* instant in the day it is handed: any
    ms inside the ET day resolves to that day, so a caller anchoring at the
    close, or at a bar in the middle of the session, gets the same answer as
    one anchoring at the open. Accepting only the exact open would make the
    parameter a checksum rather than a timestamp.

    **Not in ``app.utils.timestamps``, deliberately.** That module is
    content-hashed into all seven signal-program qualification receipts (see
    ``wiring_artifact_paths`` in ``app/engine/strategy/registry.py``, where it
    sits beside ``trading_calendar.py``), so adding a function to it flips
    those programs UNPROVEN. It lives here, with the rest of the lake's wire
    vocabulary, until that seal is deliberately re-minted.
    """
    return ny_datetime(trading_date_ms).date()


# ---------------------------------------------------------------------------
# POST-body calendar anchor — issue #1877 (PR D of #1861).
#
# start_trading_date_ms/end_trading_date_ms (below, on DataRunSpec) describe
# a *calendar*-range boundary, not an execution instant: the window may
# legitimately start or end on a weekend or a market holiday, so there is no
# session to anchor at. This is a deliberate, documented exception to the
# session-open (09:30 ET) anchor the rest of the lake's wire vocabulary uses
# (trading_date_at_ms/session_open_ms_utc above) — anchoring at 12:00:00.000
# UTC instead keeps the wire value independent of both the browser's local
# timezone and the DST-dependent ET session-open instant. A submitted value
# must land exactly on this anchor; off-anchor milliseconds are a caller
# error, not "any time near noon" to be silently snapped to the nearest date.
# ---------------------------------------------------------------------------

CALENDAR_ANCHOR_UTC_HOUR = 12
_MS_PER_DAY = 24 * 60 * 60 * 1000
_CALENDAR_ANCHOR_MS_OF_DAY = CALENDAR_ANCHOR_UTC_HOUR * 60 * 60 * 1000
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_EPOCH_DATE = date(1970, 1, 1)


def trading_date_to_calendar_anchor_ms(d: date) -> int:
    """Construct the canonical POST-body wire value for calendar date ``d``:
    ``CALENDAR_ANCHOR_UTC_HOUR``:00:00.000 UTC of that date, as int64 ms UTC.

    Pure integer arithmetic — no timezone conversion is involved, since the
    anchor is a fixed UTC hour rather than an ET wall-clock instant. The sole
    forward direction of this module's calendar-anchor pair (paired with
    :func:`calendar_anchor_ms_to_trading_date`); every ``DataRunSpec``
    construction in this codebase builds its ``start_trading_date_ms``/
    ``end_trading_date_ms`` through this helper, never through hand-written
    ``date``/``timedelta`` arithmetic of its own.
    """
    return (d - _EPOCH_DATE).days * _MS_PER_DAY + _CALENDAR_ANCHOR_MS_OF_DAY


def calendar_anchor_ms_to_trading_date(value_ms: int) -> date:
    """Inverse of :func:`trading_date_to_calendar_anchor_ms`, with validation.

    Rejects (``ValueError``) any ms value that is not exactly
    ``CALENDAR_ANCHOR_UTC_HOUR``:00:00.000 UTC of some calendar date, or that
    falls outside the representable signed-int64 range.
    """
    if not (_INT64_MIN <= value_ms <= _INT64_MAX):
        raise ValueError(f"trading-date ms {value_ms} is outside the representable signed-int64 range")
    if value_ms % _MS_PER_DAY != _CALENDAR_ANCHOR_MS_OF_DAY:
        raise ValueError(
            f"trading-date ms {value_ms} is not anchored at {CALENDAR_ANCHOR_UTC_HOUR:02d}:00:00.000 UTC"
        )
    days = value_ms // _MS_PER_DAY
    try:
        return _EPOCH_DATE + timedelta(days=days)
    except OverflowError as exc:
        raise ValueError(f"trading-date ms {value_ms} is not a representable calendar date") from exc


def is_lake_addressable_symbol(symbol: str) -> bool:
    """True iff the lake writer would accept ``symbol``.

    ``DataRunSpec`` refuses hyphenated and digit-leading tickers, so
    ``ensure_data`` can never seed one. A reader that called such a symbol a
    lake *gap* would be promising a backfill that cannot happen.
    """
    return bool(SYMBOL_RE.match(symbol))


class DataRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    run_type: Literal["python_lab", "lean_lab"]
    requester: str | None = None
    strategy_execution_id: int | None = None

    market: Literal["usa"] = "usa"
    symbols: list[str] = Field(min_length=1)
    # int64 ms UTC, anchored at CALENDAR_ANCHOR_UTC_HOUR:00:00.000 UTC (see
    # the banner above trading_date_to_calendar_anchor_ms) — the only wire
    # shape a caller may submit. There is no ISO-date compatibility alias
    # (#1877): the pre-#1877 start_trading_date/end_trading_date field names
    # are rejected by model_config's extra="forbid" below, same as any other
    # unknown field. The signed-int64 range is enforced once, inside
    # calendar_anchor_ms_to_trading_date (via _validate_calendar_anchor
    # below) alongside the anchor check itself — not restated here as a
    # second Field(ge=, le=) constraint on the same invariant.
    start_trading_date_ms: int = Field(strict=True)
    end_trading_date_ms: int = Field(strict=True)

    resolution: Literal["minute"] = "minute"
    data_types: list[Literal["trade", "quote"]] = ["trade"]
    # Deliberate subset of PriceAdjustmentMode (above): the two the fetch
    # pipeline can actually produce, not an independent copy of the
    # vocabulary. ``lean_adjusted`` is excluded because nothing derives it —
    # it would come from raw bars plus factor files, and no such producer
    # exists. Widened off ``Literal["raw"]`` by #1839, which gave the lake
    # root an adjustment segment so the two modes can coexist on disk.
    price_adjustment_mode: Literal["raw", "polygon_split_adjusted"] = "raw"
    provider: Literal["polygon"] = "polygon"

    include_factor_files: bool = True
    include_map_files: bool = True
    # Daily-trade (resolution="daily") is a whole-symbol rollup derived from
    # every complete minute-trade artifact the catalog holds for the symbol
    # — not just this call's requested window — see
    # ensure_data._process_daily_trade_artifact. It rebuilds automatically
    # whenever the symbol's catalogued coverage has grown or changed since
    # the last build (#1870), so a caller ensuring successive windows for
    # the same symbol no longer needs to opt out to avoid a collision. A
    # per-day sub-range caller (the backfill job, one day at a time) still
    # opts out per day purely to avoid rebuilding the same rollup N times in
    # a row — see backfill._day_sub_spec and its one deliberate follow-up
    # call at full range once the day loop completes. Default True preserves
    # existing single-shot-ensure behavior.
    include_daily_trade: bool = True
    # lean_image_digest is required — source of the LEAN-image-extracted
    # session calendar used by ensure_data's Phase 0 bootstrap.
    lean_image_digest: str

    fetch_timeout_seconds: int = Field(default=600, ge=10, le=7200)

    @field_validator("start_trading_date_ms", "end_trading_date_ms")
    @classmethod
    def _validate_calendar_anchor(cls, value: int) -> int:
        calendar_anchor_ms_to_trading_date(value)  # raises ValueError off-anchor
        return value

    @property
    def start_trading_date(self) -> date:
        """Internal read-only ``date`` view of ``start_trading_date_ms``,
        derived via :func:`calendar_anchor_ms_to_trading_date`. Not part of
        the wire schema — every external representation of this value is
        the ms field alone (#1877)."""
        return calendar_anchor_ms_to_trading_date(self.start_trading_date_ms)

    @property
    def end_trading_date(self) -> date:
        return calendar_anchor_ms_to_trading_date(self.end_trading_date_ms)

    @model_validator(mode="after")
    def _validate(self) -> DataRunSpec:
        # Symbols: uppercase canonical, within the catalog's storable length.
        for sym in self.symbols:
            if not is_lake_addressable_symbol(sym):
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
    """Internal identity tuple — what the catalog claim key looks like.

    Full artifact identity is ``data_root_id + price_adjustment_mode +``
    every dimension below (issue #1876 fixed design decision): the physical
    lake root an artifact belongs to, not its adjustment mode. Defaults to
    the service's configured active root (``active_root_id()``) so every
    existing caller keeps constructing identities exactly as before; a
    caller that needs another root's identity passes it explicitly.
    """

    artifact_kind: Literal["time_series_bars", "factor_file", "map_file", "metadata"]
    market: str | None = None
    symbol: str | None = None
    trading_date: date | None = None
    resolution: Literal["minute", "hour", "daily"] | None = None
    data_type: Literal["trade", "quote"] | None = None
    provider: str
    price_adjustment_mode: str | None = None
    data_root_id: UUID = Field(default_factory=active_root_id)


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
    # None for a caller that never selected it (some coverage/lookup queries
    # don't need it and skip the column) — never treat None as "verified
    # empty"; a consumer that cares (see
    # app.data_lake.run_materialization.materialize_engine_run's reused-
    # artifact verification) must skip the size check rather than fail it.
    file_size_bytes: int | None = None
    data_root_id: UUID = Field(default_factory=active_root_id)


# Named so producers upstream of ``ArtifactFailure`` can carry a reason with
# the same type the failure will be built from, instead of a bare ``str``.
ArtifactFailureReason = Literal[
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


class ArtifactFailure(BaseModel):
    artifact_kind: str
    symbol: str | None
    trading_date: date | None
    data_type: str | None
    reason: ArtifactFailureReason
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
    # The physical root's portable identity, distinct from
    # lean_data_root_path (a filesystem location for this one run) — see
    # root_identity.RootContext. Exposed so a caller can tell which root
    # every artifact below actually landed in, not just where on disk.
    data_root_id: UUID = Field(default_factory=active_root_id)
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
    # Observatory listings default to the service's configured active root
    # (issue #1876) — exposed so the response says which root it covers.
    data_root_id: UUID = Field(default_factory=active_root_id)
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
    # Exposed even when the row belongs to a root other than the service's
    # active one — artifact-by-id lookups are ID-scoped, not root-scoped
    # (issue #1876), so the response must say which root answered rather
    # than let the caller assume it was the active one.
    data_root_id: UUID = Field(default_factory=active_root_id)
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
    # Storage summaries default to the service's configured active root
    # (issue #1876) — exposed so the response says which root it covers.
    data_root_id: UUID = Field(default_factory=active_root_id)
    kinds: list[StorageKindTotal] = []
    symbols: list[SymbolCoverageSpan] = []
