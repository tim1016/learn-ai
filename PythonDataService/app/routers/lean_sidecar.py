"""LEAN Sidecar Lab — Phase 2a data-plane router.

Exposes the trusted-sample run path over HTTP so the rest of the
system (Phase 4 frontend, integration tests, manual curl) can launch
sandboxed LEAN runs without touching the launcher's Podman API
directly.

Phase 2a deliberately exposes **only** the trusted sample. The Phase 3
"Container Execution Boundary + Fidelity Boundary" gate is what unlocks
arbitrary user-source — that's tracked in the ADR §"Phase sequencing"
and refused here with a clear note in the OpenAPI schema.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from decimal import InvalidOperation as InvalidDecimalOperation
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.lean_sidecar.config import DEFAULT_ARTIFACTS_ROOT, MAX_ALGORITHM_SOURCE_BYTES
from app.lean_sidecar.cross_reconciler import (
    CrossReconciliationOutput,
    compare_cross_engine,
    internal_fill_to_dict,
)
from app.lean_sidecar.cross_runner import (
    StrategyIncompatibleError,
    StrategyNotFoundError,
    WorkspaceDataMissingError,
    run_engine_lab_on_workspace,
)
from app.lean_sidecar.diagnostics import (
    LauncherDiagnosticReport,
    run_launcher_diagnostics,
)
from app.lean_sidecar.launcher_client import (
    LauncherClientError,
    LauncherRejected,
    LauncherUnreachable,
)
from app.lean_sidecar.normalized_parser import (
    NormalizedParserError,
    NormalizedResult,
)
from app.lean_sidecar.reconciler import (
    DEFAULT_COMMISSION_ATOL,
    FeeReconciliationReport,
    reconcile_against_ibkr,
)
from app.lean_sidecar.staging import MetadataStagingError
from app.lean_sidecar.trading_calendar import (
    blocked_dates_in_range,
    is_trading_day,
    next_trading_day,
    session_open_ms_utc,
)
from app.lean_sidecar.workspace import (
    RUN_ID_PATTERN,
    TICKER_SYMBOL_PATTERN,
    SymbolValidationError,
    WorkspaceError,
    resolve_workspace,
    validate_run_id,
    validate_symbol,
)
from app.services.lean_sidecar_compare_service import (
    CompareResult,
    reconcile_trade_lists,
)
from app.services.lean_sidecar_service import (
    LeanSidecarServiceError,
    RunIdAlreadyUsedError,
    TrustedRunRequest,
    run_trusted_sample,
)

# America/New_York for the ``int64 ms UTC`` → trading-date conversion in
# manifest-derived cross-run inputs. Module-level constant so the
# allocation is amortized across requests.
_NY_TIMEZONE_FOR_DATES = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-request caps for caller-supplied inputs. Sized to match the
# project's data-availability boundary so a meaningful 422 surfaces
# before any container work.
#
# ``_MAX_TRADING_DAYS = 504`` ≈ 2 calendar years of US-equity trading
# days (252 per year). Aligns the API ceiling with Polygon.io minute-
# bar history on the project's Starter plan — LEAN is just the engine;
# the actual data ingestion ceiling is the vendor's history depth, so
# windows longer than ~2 years can't produce useful results. The
# matching ``wall_clock_timeout_s`` in ``DEFAULT_RUN_LIMITS`` is
# bumped in lockstep so the P1.1 cidfile kill switch doesn't fire
# mid-run on a legitimate 2-year backtest.
_MAX_TRADING_DAYS = 504
_MAX_STARTING_CASH = 10_000_000.0
_MIN_STARTING_CASH = 1_000.0

# Cap on the calendar/blocked-dates endpoint range so a single request
# returns at most ~2 years of dates. A buggy UI loop iterating months
# can't degrade the launcher with a 100-year sweep.
_MAX_CALENDAR_RANGE_DAYS = 366 * 2

# Window inputs are int64 ms UTC per ``.claude/rules/numerical-rigor.md``
# §"Timestamp rigor". Trading-day semantics live below this boundary
# (the orchestrator resolves the ms range into trading dates after
# converting to ET).
_MIN_EPOCH_MS = 1_000_000_000_000  # 2001-09-09 — well before any LEAN data we'd run
_MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01 — far future sanity bound


def _date_for_session_open_ms(ms: int, *, role: str) -> date:
    """Convert an int64 ms UTC value to its NY-local calendar date,
    after asserting it is exactly 09:30 ET.

    P2.5 contract: every wire ms value (``start_ms_utc``,
    ``end_ms_utc``) is the session-open millisecond — 09:30 ET of some
    calendar date, converted to UTC ms through the NY zone (DST-aware).
    Inputs that are not 09:30 ET (e.g., the pre-P2.5 midnight-UTC
    convention) are rejected here with a message naming the role and
    the offending wall-clock so the operator can fix the payload
    without reading the source.
    """
    dt_et = datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_NY_TIMEZONE_FOR_DATES)
    if dt_et.hour != 9 or dt_et.minute != 30 or dt_et.second != 0 or dt_et.microsecond != 0:
        raise ValueError(
            f"{role} must be the session-open millisecond (09:30 ET of a "
            f"trading day) per the P2.5 contract; got {dt_et.isoformat()}. "
            "See docs/handoffs/2026-05-18-design-p2-5-date-semantics-v2.md."
        )
    return dt_et.date()


def _count_weekdays_between(start_ms: int, end_ms: int) -> int:
    """Return the number of Mon-Fri days in [start_ms, end_ms].

    Trading-day approximation. The orchestrator uses the same
    weekday-only iteration when staging data, so the count agreed at
    the API boundary matches the work the staging step does.
    Holidays still count as "trading days" here — LEAN simply emits
    no bars for them, which the response surfaces via
    ``bars_consumed_by_symbol``.
    """
    if end_ms < start_ms:
        return 0
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
    days = (end - start).days + 1
    count = 0
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() < 5:
            count += 1
    return count


class _BarsSpecModel(BaseModel):
    """Pydantic shape for the ``BarsSpec`` block inside ``DataPolicy``."""

    model_config = ConfigDict(extra="forbid")

    timespan: Literal["minute", "hour", "day"]
    multiplier: int = Field(..., ge=1)


class _DataPolicyModel(BaseModel):
    """Pydantic shape for the canonical ``DataPolicy`` request block.

    Mirrors ``app.lean_sidecar.data_policy.DataPolicy`` so the router can
    accept the canonical PR B request shape directly without an extra
    adapter. The orchestrator-side dataclass is constructed by spreading
    this model's ``model_dump()`` into ``DataPolicy(**...)``.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["synthetic", "polygon"]
    symbol: str = Field(..., pattern=TICKER_SYMBOL_PATTERN.pattern)
    adjusted: bool = True
    session: Literal["regular", "extended"]
    input_bars: _BarsSpecModel
    strategy_bars: _BarsSpecModel
    timestamp_policy: Literal["bar_close_ms_utc"] = "bar_close_ms_utc"
    timezone: Literal["America/New_York"] = "America/New_York"
    provider_kind: Literal["live", "fixture"] = "live"
    fixture_id: str | None = None
    fixture_sha256: str | None = None


class TrustedRunRequestModel(BaseModel):
    """Pydantic shape for POST /api/lean-sidecar/trusted-runs.

    PR B (2026-05-19): accepts BOTH the canonical post-PR-B shape (carrying
    a ``data_policy`` block) AND the legacy top-level shape
    (``symbol``/``data_source``/``bar_minutes``/``session``/``adjustment``)
    for one deprecation cycle. Mixed-shape payloads are rejected so callers
    can't quietly drift between two contracts in the same payload.

    Phase 4c added the optional ``algorithm_source`` field — Phase 1c's
    mandatory sandbox shape (``--read-only``, ``--user=<non-root>``,
    ``--cap-drop=ALL``, ``--network=none``, workspace-only mount)
    closes the threat model that previously gated arbitrary user
    source. When omitted, the trusted sample selected by ``template`` runs.

    The endpoint name (``/trusted-runs``) is retained for backwards
    compatibility with the Phase 2a frontend; the URL no longer
    implies "trusted sample only" semantically.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        ...,
        pattern=RUN_ID_PATTERN.pattern,
        description="Slug matching ^[a-z0-9][a-z0-9_-]{2,63}$",
    )
    start_ms_utc: int = Field(
        ...,
        ge=_MIN_EPOCH_MS,
        le=_MAX_EPOCH_MS,
        description=(
            "Inclusive window start as int64 ms since Unix epoch UTC. "
            "Per .claude/rules/numerical-rigor.md, every wire timestamp "
            "is int64 ms UTC; ISO strings are not accepted."
        ),
    )
    end_ms_utc: int = Field(
        ...,
        ge=_MIN_EPOCH_MS,
        le=_MAX_EPOCH_MS,
        description=(
            "Inclusive window end as int64 ms since Unix epoch UTC. "
            "The orchestrator picks weekdays in [start, end] when staging."
        ),
    )
    starting_cash: float = Field(
        default=100_000.0,
        ge=_MIN_STARTING_CASH,
        le=_MAX_STARTING_CASH,
    )
    algorithm_source: str | None = Field(
        default=None,
        description=(
            "Optional QCAlgorithm Python source. When omitted, the "
            "bundled trusted sample selected by ``template`` runs. "
            "Must define a class named MyAlgorithm (LeanConfig's "
            "default algorithm-type-name). Capped at "
            f"{MAX_ALGORITHM_SOURCE_BYTES // 1024} KiB. Runs inside "
            "the Phase 1c sandbox shape (read-only root, non-root "
            "user, no caps, no network, workspace-only mount) — that "
            "shape is what makes accepting arbitrary source safe."
        ),
    )
    template: Literal["trusted_default", "reconciliation", "ema_crossover", "deployment_validation"] = Field(
        default="trusted_default",
        description=(
            "Phase 5b — which bundled trusted sample to stage when "
            "``algorithm_source`` is omitted. ``trusted_default`` (the "
            "back-compat default) runs the LEAN-default-brokerage "
            "sample; ``reconciliation`` runs the IBKR-brokerage-pinned "
            "sample that the Phase 5a fee reconciler returns a clean "
            "report for. Ignored when ``algorithm_source`` is provided."
        ),
    )

    # PR B canonical shape.
    data_policy: _DataPolicyModel | None = Field(
        default=None,
        description=(
            "PR B canonical data-provenance block. When provided, the "
            "legacy top-level fields below must be omitted. Mirrors "
            "``app.lean_sidecar.data_policy.DataPolicy``."
        ),
    )

    # Engine Lab parity — set when this run is the LEAN validating
    # companion of a Python engine run. Written onto the persisted
    # StrategyExecution row; the .NET persist step computes the frozen
    # ParityVerdict for the group when it lands.
    parity_group_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$",
        description="Parity group shared with the Python engine run this LEAN run validates.",
    )

    # Legacy top-level fields (one deprecation cycle).
    symbol: str | None = Field(
        default=None,
        pattern=TICKER_SYMBOL_PATTERN.pattern,
        description="DEPRECATED (PR B): use ``data_policy.symbol``.",
    )
    data_source: Literal["synthetic", "polygon"] | None = Field(
        default=None,
        description="DEPRECATED (PR B): use ``data_policy.source``.",
    )
    bar_minutes: int | None = Field(
        default=None,
        ge=1,
        description="DEPRECATED (PR B): use ``data_policy.strategy_bars.multiplier``.",
    )
    session: Literal["regular", "extended"] | None = Field(
        default=None,
        description="DEPRECATED (PR B): use ``data_policy.session``.",
    )
    adjustment: Literal["raw", "adjusted"] | None = Field(
        default=None,
        description=(
            "DEPRECATED (PR B): use ``data_policy.adjusted`` (bool). "
            "Legacy values: ``'raw'`` -> adjusted=False; ``'adjusted'`` -> adjusted=True."
        ),
    )

    @model_validator(mode="after")
    def _normalize_to_data_policy(self) -> TrustedRunRequestModel:
        """Synthesize ``data_policy`` from legacy fields and reject mixed shapes."""
        legacy_present = any(
            v is not None for v in (self.symbol, self.data_source, self.bar_minutes, self.session, self.adjustment)
        )
        if self.data_policy is not None and legacy_present:
            raise ValueError(
                "Cannot mix top-level legacy fields (symbol/data_source/bar_minutes/"
                "session/adjustment) with a ``data_policy`` block; choose one shape."
            )
        if self.data_policy is None:
            # Synthesize from legacy fields. To preserve PR A's
            # one-deprecation-cycle compatibility guarantee, missing
            # legacy fields fall back to PR A's defaults rather than
            # 422-ing — the existing Lean Lab UI posts only
            # ``run_id``/``symbol``/window/cash/template. ``symbol`` is
            # the one field with no sensible default (it's the asset
            # being traded), so its absence still raises.
            if self.symbol is None:
                raise ValueError("When ``data_policy`` is omitted, ``symbol`` is required (legacy shape).")
            # Legacy-shape defaults match PR A. ``adjustment`` defaults
            # to ``"raw"`` here (NOT to ``adjusted=True``) — the
            # pre-PR-B wire vocabulary's implicit value was ``"raw"``,
            # and silently switching legacy callers to ``adjusted=True``
            # would break the one-cycle compat promise. New callers
            # that want ``adjusted=True`` send the full ``data_policy``
            # block (where ``adjusted: bool = True`` is the field
            # default).
            legacy_data_source = self.data_source if self.data_source is not None else "synthetic"
            legacy_bar_minutes = self.bar_minutes if self.bar_minutes is not None else 15
            legacy_session = self.session if self.session is not None else "regular"
            legacy_adjustment = self.adjustment if self.adjustment is not None else "raw"
            adjusted = legacy_adjustment == "adjusted"
            object.__setattr__(
                self,
                "data_policy",
                _DataPolicyModel(
                    source=legacy_data_source,
                    symbol=self.symbol.upper(),
                    adjusted=adjusted,
                    session=legacy_session,
                    input_bars=_BarsSpecModel(timespan="minute", multiplier=1),
                    strategy_bars=_BarsSpecModel(timespan="minute", multiplier=legacy_bar_minutes),
                ),
            )
            logger.warning(
                "TrustedRunRequest using legacy top-level shape; convert to data_policy block. run_id=%s",
                self.run_id,
            )
        self._validate_window_normalized()
        return self

    def _validate_window_normalized(self) -> None:
        """Validate window + symbol + algorithm_source using ``data_policy``."""
        if self.end_ms_utc <= self.start_ms_utc:
            raise ValueError("end_ms_utc must be strictly greater than start_ms_utc")
        # P2.5 contract: both endpoints are 09:30 ET (session-open) ms,
        # half-open [start, end). The window's last full session is
        # the trading day immediately preceding the exclusive end.
        # Weekends and holidays BETWEEN the endpoints are allowed
        # (staging skips them). Early-close half-days are trading
        # sessions and are staged like any other session.
        start_date = _date_for_session_open_ms(self.start_ms_utc, role="start_ms_utc")
        exclusive_end_date = _date_for_session_open_ms(self.end_ms_utc, role="end_ms_utc")
        if not is_trading_day(start_date):
            raise ValueError(
                f"start_ms_utc resolves to {start_date.isoformat()} which is not a trading day (weekend or holiday)"
            )
        if not is_trading_day(exclusive_end_date):
            raise ValueError(
                f"end_ms_utc resolves to {exclusive_end_date.isoformat()} which "
                "is not a trading day (the exclusive end must be a session-open)"
            )
        # Resolve the inclusive end_date: the trading day immediately
        # preceding exclusive_end_date (skipping weekends/holidays).
        end_date = exclusive_end_date - timedelta(days=1)
        while not is_trading_day(end_date):
            end_date -= timedelta(days=1)
            if end_date < start_date:
                raise ValueError(
                    f"no trading days in window [{start_date.isoformat()}, {exclusive_end_date.isoformat()})"
                )
        # Pre-launcher cap on trading-day count (the launcher's
        # wall-clock timeout and the synthetic-bar generator both
        # scale with the count).
        trading_days = 0
        d = start_date
        while d <= end_date:
            if is_trading_day(d):
                trading_days += 1
            d += timedelta(days=1)
        if trading_days > _MAX_TRADING_DAYS:
            raise ValueError(f"window spans {trading_days} trading days; max is {_MAX_TRADING_DAYS}")
        if trading_days == 0:
            raise ValueError("window contains no trading days — staging would produce zero bars")
        # Symbol must pass the full validator — the field-level
        # regex catches ``/``, ``\``, length, and the alphabet, but
        # not the dot-only case (``"."``, ``".."``). validate_symbol
        # closes that hole and is the same function the staging
        # writers re-check against.
        assert self.data_policy is not None  # synthesized by _normalize_to_data_policy
        try:
            validate_symbol(self.data_policy.symbol)
        except SymbolValidationError as e:
            raise ValueError(str(e)) from e
        # Phase 4c: algorithm_source validation. Size cap is the
        # ADR's per-request hard limit; UTF-8-ness is checked
        # implicitly by Pydantic accepting str. The MyAlgorithm
        # class-name requirement is documented but not regex-
        # enforced here — LEAN's launcher fails fast on a missing
        # class with a clear "algorithm-type-name not found" error,
        # which the result_classifier picks up as `runtime_error`.
        # Text-level "no `import os`" filtering would be security
        # theater: the Phase 1c sandbox is the boundary, not the
        # source-text contents.
        if self.algorithm_source is not None:
            if not self.algorithm_source.strip():
                raise ValueError("algorithm_source, if provided, must not be empty/whitespace")
            source_bytes = len(self.algorithm_source.encode("utf-8"))
            if source_bytes > MAX_ALGORITHM_SOURCE_BYTES:
                raise ValueError(
                    f"algorithm_source is {source_bytes} bytes; "
                    f"max is {MAX_ALGORITHM_SOURCE_BYTES} bytes "
                    f"({MAX_ALGORITHM_SOURCE_BYTES // 1024} KiB)"
                )


class RunSummaryModel(BaseModel):
    """One row in the run-history index.

    Built from each run's ``manifest.json``. Index reads do not touch
    the launcher and do not require LEAN to be running — they're a
    pure read over the artifacts root. Fields are the minimum needed
    to render a sidebar row + offer a "click to re-open" action;
    detail views still go through the existing per-run endpoints
    (``/runs/{id}/manifest``, ``/runs/{id}/normalized``, etc.).
    """

    run_id: str
    symbol: str | None
    requested_start_ms_utc: int | None
    requested_end_ms_utc: int | None
    started_at_ms: int | None
    finished_at_ms: int | None
    exit_code: int | None
    algorithm_source_kind: Literal["trusted_sample", "user_provided", "unknown"]
    # Compact derived flag the UI can branch on without re-fetching the
    # normalized result. ``True`` when ``exit_code == 0``; ``None`` when
    # the run never wrote a finished_at_ms (likely still running or
    # crashed mid-launch). Not a substitute for ``is_clean`` — LEAN can
    # exit 0 with classified errors — but a fast at-a-glance signal.
    exit_clean: bool | None
    # The true cleanliness signal: extracted from the manifest's
    # ``is_clean=<bool>`` note, which the service writes from the
    # launcher's response. ``None`` for legacy manifests (Phase 1) that
    # predate the note. The Phase 4d/4e sidebar uses THIS field (not
    # ``exit_clean``) when synthesizing a rehydrated TrustedRunResponse
    # so a run that exited 0 with classified LEAN errors does not paint
    # as a green "Clean run."
    is_clean: bool | None
    # Phase 4f: which LEAN error categories appeared in the run's
    # log.txt, parsed from the manifest's
    # ``lean_error_categories=[...]`` note. Empty list means the run
    # had no categorized LEAN errors. The Phase 4d/4e sidebar uses
    # this to populate the rehydrated TrustedRunResponse with
    # bucket-name placeholders (the manifest doesn't store individual
    # lines, only the bucket names), so a rehydrated run with
    # `is_clean=false` shows WHICH category was hit instead of an
    # uninformative "errors logged" badge with empty buckets.
    lean_error_categories: list[str]


class RunIndexResponseModel(BaseModel):
    """Paged-ish response for the run-history index.

    ``cap`` is the configured per-request cap; ``truncated`` is True if
    the artifacts root holds more runs than were returned. The frontend
    surfaces both so the operator knows the list is not necessarily
    exhaustive.
    """

    runs: list[RunSummaryModel]
    cap: int
    truncated: bool


class LeanErrorsResponseModel(BaseModel):
    """Mirror of LaunchResponse.lean_errors with the launcher's stable
    category keys. Exposed as a separate model so OpenAPI documents it."""

    analysis_failed: list[str] = Field(default_factory=list)
    failed_data_requests: list[str] = Field(default_factory=list)
    runtime_error: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


class TrustedRunResponseModel(BaseModel):
    """The response shape callers branch on.

    ``is_clean`` is the single boolean the caller should branch on. The
    other fields exist for human/operator inspection.
    """

    run_id: str
    is_clean: bool
    exit_code: int
    duration_ms: int
    timed_out: bool
    lean_errors: LeanErrorsResponseModel
    log_tail: str
    manifest_path: str
    workspace_root: str
    observations_path: str
    lean_log_path: str
    # Phase 3a: present when LEAN produced parseable output. ``None``
    # when the run crashed before producing artifacts (the operator
    # then falls back to /runs/{id}/log for diagnosis).
    normalized_path: str | None = None
    normalized_parser_version: str | None = None
    total_order_events: int | None = None
    total_equity_points: int | None = None
    # Task 1.10: the StrategyExecution.Id assigned by the .NET backend.
    # ``None`` when persistence failed (logged; the run is not aborted).
    strategy_execution_id: int | None = None


@router.post(
    "/trusted-runs",
    response_model=TrustedRunResponseModel,
    status_code=status.HTTP_200_OK,
    summary="Run the trusted buy-and-hold sample through the LEAN sidecar.",
)
async def post_trusted_run(payload: TrustedRunRequestModel) -> TrustedRunResponseModel:
    """Stage, launch, and write the manifest for one trusted-sample run.

    Phase 2a: trusted sample only. No algorithm-source field, no
    arbitrary user input. See ADR §"Phase sequencing" for when that
    gate opens (Phase 3).
    """
    # PR B: ``payload.data_policy`` is either posted by the caller
    # directly or synthesized from legacy fields by
    # ``TrustedRunRequestModel._normalize_to_data_policy``. Build the
    # orchestrator-side ``DataPolicy`` dataclass by spreading the
    # validated Pydantic block — its field names match the dataclass
    # 1:1 (BarsSpec sub-shape included).
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    assert payload.data_policy is not None  # invariant after _normalize_to_data_policy
    dp_model = payload.data_policy
    data_policy = DataPolicy(
        source=dp_model.source,
        symbol=dp_model.symbol,
        adjusted=dp_model.adjusted,
        session=dp_model.session,
        input_bars=BarsSpec(timespan=dp_model.input_bars.timespan, multiplier=dp_model.input_bars.multiplier),
        strategy_bars=BarsSpec(
            timespan=dp_model.strategy_bars.timespan,
            multiplier=dp_model.strategy_bars.multiplier,
        ),
        timestamp_policy=dp_model.timestamp_policy,
        timezone=dp_model.timezone,
        provider_kind=dp_model.provider_kind,
        fixture_id=dp_model.fixture_id,
        fixture_sha256=dp_model.fixture_sha256,
    )
    request = TrustedRunRequest(
        run_id=payload.run_id,
        start_ms_utc=payload.start_ms_utc,
        end_ms_utc=payload.end_ms_utc,
        starting_cash=payload.starting_cash,
        algorithm_source=payload.algorithm_source,
        template=payload.template,
        data_policy=data_policy,
        parity_group_id=payload.parity_group_id,
    )
    try:
        result = await run_trusted_sample(request)
    except LauncherRejected as e:
        # The launcher is the security boundary — its 400s should
        # surface as 400s to our caller with the same ``reason`` so
        # the caller can branch identically.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": e.reason, "message": e.message},
        ) from e
    except LauncherUnreachable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "launcher_unreachable", "message": str(e)},
        ) from e
    except LauncherClientError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "launcher_protocol_error", "message": str(e)},
        ) from e
    except MetadataStagingError as e:
        # ``stage_lean_metadata_from_image`` raises this when the
        # launcher can't extract market-hours-database.json /
        # symbol-properties-database.csv from the pinned LEAN image —
        # the launcher process is down, the pinned digest doesn't
        # match anything locally, or ``podman create/cp`` fails inside
        # the launcher. Without this clause the exception escapes
        # through the global ``Exception`` handler as a 500, and the
        # browser surfaces it as a misleading CORS error because the
        # response that *did* reach Starlette's error path bypasses
        # the CORS middleware's simple_response. Mapping to an
        # explicit 502 here keeps the CORS headers attached and lets
        # the frontend branch on a stable ``reason`` label.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "metadata_staging_failed", "message": str(e)},
        ) from e
    except RunIdAlreadyUsedError as e:
        # 409 Conflict: a run with this run_id already exists on disk.
        # The caller MUST pick a fresh slug — the existing run's
        # artifacts must not be silently overwritten because the
        # parser then reads stale ``*-summary.json`` content under a
        # freshly-written manifest. Subclass of LeanSidecarServiceError
        # so this except must come BEFORE the generic catch below.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "run_id_already_used", "message": str(e)},
        ) from e
    except LeanSidecarServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": "service_error", "message": str(e)},
        ) from e

    return TrustedRunResponseModel(
        run_id=result.run_id,
        is_clean=result.is_clean,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        lean_errors=LeanErrorsResponseModel(
            analysis_failed=result.lean_errors.get("analysis_failed", []),
            failed_data_requests=result.lean_errors.get("failed_data_requests", []),
            runtime_error=result.lean_errors.get("runtime_error", []),
            other=result.lean_errors.get("other", []),
        ),
        log_tail=result.log_tail,
        manifest_path=str(result.manifest_path),
        workspace_root=str(result.workspace_root),
        observations_path=str(result.observations_path),
        lean_log_path=str(result.lean_log_path),
        normalized_path=str(result.normalized_path) if result.normalized_path else None,
        normalized_parser_version=(result.normalized.parser_version if result.normalized else None),
        total_order_events=(result.normalized.total_order_events if result.normalized else None),
        total_equity_points=(result.normalized.total_equity_points if result.normalized else None),
        strategy_execution_id=result.strategy_execution_id,
    )


# ---------------------------------------------------------------------------
# Run-history index
# ---------------------------------------------------------------------------

# Cap on rows returned by GET /runs. Single host with a single operator
# rarely accumulates more than a few hundred runs, but the cap stops a
# pathological artifacts root from ballooning the response. The frontend
# sees ``truncated=True`` and can offer a "show older" follow-up later.
_RUN_INDEX_CAP = 200

# Safety bound on how many manifests we'll load before sorting. The
# display cap above is what the operator sees; this is the work cap.
# 5× display cap gives the sort enough headroom to pick the truly-
# newest runs without unbounded I/O on a runaway artifacts root.
_SCAN_HARD_CAP = _RUN_INDEX_CAP * 5


def _safe_load_manifest_summary(manifest_path) -> dict | None:
    """Read one manifest.json and return a flat dict for the index row.

    Returns ``None`` if the file does not exist, is not valid JSON, or
    is missing fields required to build a row. The index endpoint
    treats unreadable manifests as "skip silently" — a half-written
    manifest from a crash mid-run should not break the listing.
    """
    import json

    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    requested = data.get("requested_window_ms") or {}
    params = data.get("parameters") or {}
    notes = data.get("notes") or []
    # ``algorithm_source_kind`` was added to manifest.notes in Phase 4c.
    # Older manifests don't have it; treat them as "unknown" rather
    # than guessing — guessing creates misleading sidebar copy.
    # ``is_clean`` is similarly note-encoded (since Phase 2a's manifest
    # writer); ``None`` for pre-Phase-2a manifests.
    kind = "unknown"
    is_clean: bool | None = None
    lean_error_categories: list[str] = []
    for note in notes:
        if not isinstance(note, str):
            continue
        if note.startswith("algorithm_source_kind="):
            value = note.split("=", 1)[1]
            if value in ("trusted_sample", "user_provided"):
                kind = value
        elif note.startswith("is_clean="):
            value = note.split("=", 1)[1]
            if value == "True":
                is_clean = True
            elif value == "False":
                is_clean = False
            # Anything else stays None — never silently coerce a
            # malformed note into a truthy/falsy value.
        elif note.startswith("lean_error_categories="):
            lean_error_categories = _parse_categories_note(note.split("=", 1)[1])
    exit_code = data.get("exit_code")
    return {
        "symbol": params.get("symbol") if isinstance(params, dict) else None,
        "requested_start_ms_utc": requested.get("start_ms") if isinstance(requested, dict) else None,
        "requested_end_ms_utc": requested.get("end_ms") if isinstance(requested, dict) else None,
        "started_at_ms": data.get("started_at_ms"),
        "finished_at_ms": data.get("finished_at_ms"),
        "exit_code": exit_code,
        "algorithm_source_kind": kind,
        "exit_clean": (exit_code == 0) if exit_code is not None else None,
        "is_clean": is_clean,
        "lean_error_categories": lean_error_categories,
    }


# Known LEAN error bucket keys per the launcher's classifier. The note
# is whitelisted against this set so a malformed note ("=junk") can't
# inject arbitrary strings into the sidebar.
_VALID_LEAN_ERROR_CATEGORIES = frozenset({"analysis_failed", "failed_data_requests", "runtime_error", "other"})


def _parse_categories_note(raw: str) -> list[str]:
    """Parse the ``lean_error_categories=['x', 'y']`` value into a list.

    The service writes ``f"lean_error_categories={sorted(response.lean_errors.keys())}"``
    which python's str() on a list produces (single-quoted entries).
    Parsing safely: strip brackets, split on commas, strip quotes +
    whitespace, keep only known categories. Returns an empty list when
    the format is unrecognized — a malformed note must not crash the
    index endpoint.
    """
    if not raw.startswith("[") or not raw.endswith("]"):
        return []
    inner = raw[1:-1].strip()
    if not inner:
        return []
    out: list[str] = []
    for piece in inner.split(","):
        cleaned = piece.strip().strip("'").strip('"').strip()
        if cleaned in _VALID_LEAN_ERROR_CATEGORIES:
            out.append(cleaned)
    return out


@router.get(
    "/runs",
    response_model=RunIndexResponseModel,
    summary="List past runs from the artifacts root (newest first).",
)
async def get_runs_index() -> RunIndexResponseModel:
    """Return the run-history index for the LEAN Lab sidebar.

    Scans direct child directories of ``DEFAULT_ARTIFACTS_ROOT``, keeps
    those whose names match ``RUN_ID_PATTERN`` (so stray dirs created
    out-of-band are ignored), reads each ``manifest.json``, sorts by
    ``started_at_ms`` desc (run_id desc as a stable tiebreaker), and
    truncates to ``_RUN_INDEX_CAP``.

    Reviewer P2: cap is applied *after* the sort, not during the scan.
    The scan-time ordering is by ``run_id`` text, which can diverge
    from ``started_at_ms`` order — pre-Phase-4d run_ids didn't include
    a millisecond suffix, so a legacy run with a lexically-late slug
    could push a genuinely-newer run past the cap. Sorting first and
    truncating after costs O(N log N) on the row count but is bounded
    by ``_SCAN_HARD_CAP`` to keep a pathological artifacts root from
    DoSing the endpoint.

    Pure read — does not touch the launcher, does not require LEAN to
    be running. Manifests that fail to parse are silently skipped (a
    half-written file from a crash mid-write shouldn't break the
    listing for the rest).
    """
    rows: list[RunSummaryModel] = []
    if not DEFAULT_ARTIFACTS_ROOT.exists():
        return RunIndexResponseModel(runs=[], cap=_RUN_INDEX_CAP, truncated=False)
    candidate_dirs = []
    for entry in DEFAULT_ARTIFACTS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        try:
            validate_run_id(entry.name)
        except WorkspaceError:
            continue
        candidate_dirs.append(entry)
    # Sort BEFORE truncating to the work cap. Reviewer P2: filesystem
    # iteration order is not guaranteed (POSIX makes no promise; on
    # ext4 it's hash-order, on NTFS+podman-bind it can be insertion-
    # order, on tmpfs it's arbitrary). Slicing an unsorted list to
    # ``_SCAN_HARD_CAP`` could drop genuinely-newer runs and then sort
    # only the kept subset — making the sidebar miss recent activity
    # once the artifacts root grows past 5× the display cap.
    #
    # Sort the FULL candidate_dirs by run_id-desc (modern slug-prefix
    # = timestamp-ish), then take the first _SCAN_HARD_CAP. The
    # manifest-timestamp sort runs after manifests are loaded; using
    # the run_id pre-sort just biases the truncation toward newest-by-
    # name. _SCAN_HARD_CAP = 5× display cap leaves enough headroom
    # that even out-of-order timestamps within the prefix can be
    # re-sorted to the right place.
    candidate_dirs.sort(key=lambda p: p.name, reverse=True)
    scan_dirs = candidate_dirs[:_SCAN_HARD_CAP]
    for entry in scan_dirs:
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        summary = _safe_load_manifest_summary(manifest_path)
        if summary is None:
            continue
        try:
            rows.append(RunSummaryModel(run_id=entry.name, **summary))
        except ValidationError as e:
            # Manifest parsed as JSON but a typed field is malformed
            # (e.g., ``started_at_ms="invalid"``). Skip the row so the
            # index stays responsive; log with context so the operator
            # can find the bad workspace. Per .claude/CLAUDE.md we
            # never silently swallow exceptions.
            logger.warning(
                "Skipping run %s in index: manifest schema invalid (%s)",
                entry.name,
                e,
            )
    # Sort all loaded rows by started_at_ms desc; runs without that
    # field fall back to run_id desc.
    rows.sort(
        key=lambda r: (r.started_at_ms if r.started_at_ms is not None else -1, r.run_id),
        reverse=True,
    )
    truncated = len(rows) > _RUN_INDEX_CAP or len(candidate_dirs) > _SCAN_HARD_CAP
    return RunIndexResponseModel(runs=rows[:_RUN_INDEX_CAP], cap=_RUN_INDEX_CAP, truncated=truncated)


# ---------------------------------------------------------------------------
# Inspection endpoints
# ---------------------------------------------------------------------------
#
# These read artifacts from an existing run's workspace. They never
# touch the launcher; they only serve files the launcher (or the
# orchestrator) already wrote.


def _resolved_workspace_or_404(run_id: str):
    """Resolve the run_id to a workspace or raise 404.

    The same path-under-root contract as :func:`resolve_workspace` —
    so a slug like ``../escape`` cannot be smuggled into a workspace
    outside the artifacts root.
    """
    try:
        workspace = resolve_workspace(run_id, DEFAULT_ARTIFACTS_ROOT)
    except WorkspaceError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": "invalid_run_id", "message": str(e)},
        ) from e
    if not workspace.workspace_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "run_not_found", "message": f"no workspace for {run_id}"},
        )
    return workspace


@router.get(
    "/calendar/blocked-dates",
    summary="Return blocked non-tradeable dates in a range.",
)
async def get_calendar_blocked_dates(
    from_: date = Query(
        ...,
        alias="from",
        description="Inclusive start date (YYYY-MM-DD)",
    ),
    to: date = Query(..., description="Inclusive end date (YYYY-MM-DD)"),
) -> dict:
    """Return weekends and holidays in ``[from_, to]``.

    Per P2.5 the UI date picker consumes this to disable + label each
    blocked date — single backend source of truth for the NYSE
    calendar so the picker and the validator cannot drift.

    Early-close half-days are tradeable sessions and are NOT returned
    here. Weekends and holidays return ``"weekend"`` and ``"holiday"``
    respectively. Trading days are NOT in the payload.
    """
    if to < from_:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "invalid_range",
                "message": (f"`to` ({to.isoformat()}) must not be before `from` ({from_.isoformat()})"),
            },
        )
    span_days = (to - from_).days + 1
    if span_days > _MAX_CALENDAR_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "range_too_large",
                "message": (
                    f"range is {span_days} days; max is "
                    f"{_MAX_CALENDAR_RANGE_DAYS}. Paginate the picker if "
                    "more is needed."
                ),
            },
        )
    blocked = blocked_dates_in_range(from_, to)
    return {
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "blocked": [{"date": d.isoformat(), "reason": reason} for d, reason in sorted(blocked.items())],
    }


@router.get(
    "/calendar/next-trading-day-open",
    summary="Return the next NYSE session-open after a given date.",
)
async def get_calendar_next_trading_day_open(
    date_: date = Query(
        ...,
        alias="date",
        description="Reference date (YYYY-MM-DD); the response returns the next session strictly after this date.",
    ),
) -> dict:
    """Return the next trading session's date and 09:30 ET open as int64 ms UTC.

    P2.5 contract: ``end_ms_utc`` is the half-open window's exclusive
    end, which must be 09:30 ET of the trading day after the operator's
    chosen end date. The frontend calls this endpoint so the picker's
    end-date selection can be advanced to the canonical exclusive end
    without re-implementing the NYSE calendar client-side.

    Output ``session_open_ms_utc`` is DST-aware (delegated to
    ``trading_calendar.session_open_ms_utc`` which goes through
    ``ZoneInfo("America/New_York")``).
    """
    try:
        next_date = next_trading_day(date_)
    except LookupError as e:
        # 14-day forward window exhausted — practically only reachable
        # with date arithmetic far outside the calendar's range. Surface
        # it as 422 so the caller knows the input is the problem, not
        # the server.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "no_session_in_range",
                "message": str(e),
            },
        ) from e
    return {
        "next_trading_date": next_date.isoformat(),
        "session_open_ms_utc": session_open_ms_utc(next_date),
    }


@router.get(
    "/runs/{run_id}/manifest",
    summary="Return the reproducibility manifest for a completed run.",
)
async def get_manifest(run_id: str) -> dict:
    workspace = _resolved_workspace_or_404(run_id)
    if not workspace.manifest_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "manifest_missing",
                "message": f"manifest.json not yet written for {run_id}",
            },
        )
    import json

    return json.loads(workspace.manifest_path.read_text(encoding="utf-8"))


@router.get(
    "/runs/{run_id}/observations",
    response_class=PlainTextResponse,
    summary="Return the trusted sample's per-bar audit CSV.",
)
async def get_observations(run_id: str) -> PlainTextResponse:
    workspace = _resolved_workspace_or_404(run_id)
    obs_path = workspace.object_store_dir / "observations.csv"
    if not obs_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "observations_missing",
                "message": f"observations.csv not present for {run_id}",
            },
        )
    return PlainTextResponse(obs_path.read_text(encoding="utf-8"))


_LEAN_LOG_TAIL_MAX_BYTES = 1 << 20  # 1 MiB


@router.get(
    "/runs/{run_id}/normalized",
    summary="Return the normalized LEAN result (parsed equity curve + orders + stats).",
)
async def get_normalized(run_id: str) -> dict:
    """Serve the parsed result.json written by the orchestrator.

    404 when the file is absent: either the run hasn't completed, or
    LEAN died before producing the artifacts the parser reads. The
    operator can `GET /runs/{id}/log` to diagnose.
    """
    workspace = _resolved_workspace_or_404(run_id)
    result_path = workspace.normalized_dir / "result.json"
    if not result_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "normalized_missing",
                "message": (
                    f"normalized result.json not present for {run_id}; "
                    "LEAN may have failed before producing parseable output"
                ),
            },
        )
    import json

    return json.loads(result_path.read_text(encoding="utf-8"))


class FeeDivergenceModel(BaseModel):
    """One row in the fee reconciliation report. Decimals serialized as
    strings so the wire is exact (avoids float-binary error in JSON)."""

    order_event_id: int
    order_id: int
    symbol: str
    ms_utc: int
    fill_quantity: int
    fill_price: str
    recorded_fee: str | None
    expected_ibkr_fee: str
    # ``None`` when ``category == "no_recorded_fee"`` or
    # ``fractional_quantity`` — there's nothing meaningful to subtract.
    # Non-null for every ``commission_drift`` row.
    delta: str | None
    category: Literal["commission_drift", "no_recorded_fee", "fractional_quantity"]
    # Populated only when category == "fractional_quantity" — carries
    # the original float so the operator can see what LEAN emitted
    # before integer rounding would have been applied.
    fill_quantity_raw: float | None = None


class RunReconciliationReportModel(BaseModel):
    """Phase 5a — categorized fee-divergence report for one LEAN Lab run.

    The report is decoupled from whether the run was reconciliation-grade.
    A default-brokerage trusted-sample run will naturally surface many
    ``commission_drift`` rows because LEAN's default commission differs
    from IBKR's tier — that signal is informative, not a bug.
    """

    run_id: str
    algorithm_id: str
    # Parser-version pin recorded with the result.json the report was
    # computed from. Surfaces here so a downstream consumer can tell
    # whether two reconciliation reports are comparable (different
    # parser_version means the upstream normalization may differ).
    normalized_parser_version: str
    total_fill_events: int
    matched_count: int
    divergent_count: int
    # All Decimals on the wire are strings — preserves exact cents and
    # documents the tolerance regime in numerical-rigor.md.
    commission_atol: str
    total_recorded_fees: str
    total_expected_ibkr_fees: str
    divergences: list[FeeDivergenceModel]


def _report_to_model(
    report: FeeReconciliationReport,
    *,
    algorithm_id: str,
    normalized_parser_version: str,
) -> RunReconciliationReportModel:
    return RunReconciliationReportModel(
        run_id=report.run_id,
        algorithm_id=algorithm_id,
        normalized_parser_version=normalized_parser_version,
        total_fill_events=report.total_fill_events,
        matched_count=report.matched_count,
        divergent_count=report.divergent_count,
        commission_atol=str(report.commission_atol),
        total_recorded_fees=str(report.total_recorded_fees),
        total_expected_ibkr_fees=str(report.total_expected_ibkr_fees),
        divergences=[
            FeeDivergenceModel(
                order_event_id=d.order_event_id,
                order_id=d.order_id,
                symbol=d.symbol,
                ms_utc=d.ms_utc,
                fill_quantity=d.fill_quantity,
                fill_price=str(d.fill_price),
                recorded_fee=None if d.recorded_fee is None else str(d.recorded_fee),
                expected_ibkr_fee=str(d.expected_ibkr_fee),
                delta=None if d.delta is None else str(d.delta),
                category=d.category.value,
                fill_quantity_raw=d.fill_quantity_raw,
            )
            for d in report.divergences
        ],
    )


@router.post(
    "/runs/{run_id}/reconcile",
    response_model=RunReconciliationReportModel,
    summary="Reconcile a past run's recorded fees against the canonical IBKR commission model (Phase 5a).",
)
async def post_reconcile(run_id: str) -> RunReconciliationReportModel:
    """Phase 5a — self-reconciliation: compares each filled order
    event's recorded ``orderFeeAmount`` against the IBKR equity-tier
    fee. Returns the categorized divergence report; tolerance is the
    project default ($0.01) from ``.claude/rules/numerical-rigor.md``.

    Accepts only ``run_id``. The reconciler is decoupled from whether
    the run was reconciliation-grade — a default-brokerage run will
    have many ``commission_drift`` rows by construction (LEAN's
    default commission ≠ IBKR's tier). Phase 5b will add the
    reconciliation-grade template that makes this report come back
    clean for properly-pinned runs.

    Reads the persisted normalized ``result.json`` (written by the
    orchestrator after each run), NOT a fresh re-parse of LEAN's raw
    output artifacts. The persisted file pins the parser_version at the
    time of the run; reading it back means a future parser-version bump
    cannot retroactively alter the reconciliation result for an old
    run. The pinned ``parser_version`` is echoed back on the response
    so a consumer can detect when two reports are not comparable.

    404 contract: ``normalized_missing`` if ``result.json`` is absent
    (run hadn't completed, or LEAN crashed before producing parseable
    output) OR if the file exists but does not validate against the
    current ``NormalizedResult`` schema.
    """
    workspace = _resolved_workspace_or_404(run_id)
    result_path = workspace.normalized_dir / "result.json"
    if not result_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "normalized_missing",
                "message": (
                    f"cannot reconcile {run_id}: normalized result.json not present. "
                    f"The run may have crashed before producing parseable output."
                ),
            },
        )
    try:
        result = NormalizedResult.model_validate_json(
            result_path.read_text(encoding="utf-8"),
        )
    except (OSError, ValueError, NormalizedParserError) as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "normalized_missing",
                "message": (f"cannot reconcile {run_id}: result.json failed to load ({e})."),
            },
        ) from e
    # Reviewer P1: pass the path-parameter run_id directly so the report's
    # ``run_id`` is the workspace slug (what the caller queried), NOT the
    # algorithm-type-name. They diverge whenever LEAN's ``algorithm-id``
    # differs from the workspace slug (i.e., always, since the slug is a
    # UI-generated UUID-ish token and the algorithm-id defaults to
    # ``MyAlgorithm``). Algorithm-id is still exposed as a separate field.
    report = reconcile_against_ibkr(
        run_id=run_id,
        order_events=result.order_events,
        commission_atol=DEFAULT_COMMISSION_ATOL,
    )
    return _report_to_model(
        report,
        algorithm_id=result.algorithm_id,
        normalized_parser_version=result.parser_version,
    )


# ---------------------------------------------------------------------------
# Phase 5g — cross-engine reconciliation scaffold
# ---------------------------------------------------------------------------
#
# Phase 5a's POST /runs/{id}/reconcile is *self*-reconciliation (LEAN's
# recorded fees vs the canonical IbkrEquityCommissionModel). Phase 5g is
# *cross-engine* reconciliation: diff this LEAN-Lab run's fills against the
# Engine Lab's fills for the caller-named strategy class on the same
# workspace data.
#
# Phase 5g.3: endpoint + Pydantic request/response shapes are wired to
# the Engine-Lab cross-run primitive and the DivergenceCategory diff.
# Earlier Phase 5g.1 scaffold returned 501; this route now returns a
# real CrossEngineReconciliationReportModel on the happy path.
#
# Design notes (resolved via mission-critical doc D3, 2026-05-18):
#   * Pairing is caller-supplied — no auto-derivation. The request names
#     the Engine Lab strategy class.
#   * Default gating taxonomy: every DivergenceCategory is gating EXCEPT
#     COMMISSION_DRIFT, which is diagnostic by default. Caller may opt in
#     via assert_fees=true (which only makes sense on reconciliation-grade
#     templates where the IBKR fee model is pinned on both sides).
#   * The response carries an explicit schema_version (D10) so future
#     shape changes are detectable on the consumer side.

# Valid DivergenceCategory values, kept in lockstep with
# ``research.parity.qc_reconciler.DivergenceCategory``. Re-imported here
# so the Pydantic Literal can pin the wire enumeration without making
# the router depend at runtime on the qc_reconciler package.
_CROSS_ENGINE_DIVERGENCE_CATEGORIES = (
    "fixture_insufficient",
    "decision_mismatch",
    "direction_mismatch",
    "quantity_mismatch",
    "fill_price_drift",
    "commission_drift",
    "pnl_drift",
    "order_type_mismatch",
)
CrossEngineDivergenceCategory = Literal[
    "fixture_insufficient",
    "decision_mismatch",
    "direction_mismatch",
    "quantity_mismatch",
    "fill_price_drift",
    "commission_drift",
    "pnl_drift",
    "order_type_mismatch",
]


class CrossReconcileRequestModel(BaseModel):
    """POST /api/lean-sidecar/runs/{run_id}/cross-reconcile — request shape.

    The request names which Engine Lab strategy class to diff against.
    No auto-derivation: per D3, ambiguity at this seam silently produces
    wrong divergence reports, so we require an explicit string.
    """

    model_config = ConfigDict(extra="forbid")

    engine_lab_strategy_class: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "Name of the Engine Lab strategy class to run on the same "
            "workspace data and diff against this LEAN-Lab run. Required: "
            "no auto-derivation convention. See mission-critical doc D3."
        ),
    )
    assert_fees: bool = Field(
        default=False,
        description=(
            "When false (default), COMMISSION_DRIFT is diagnostic — it "
            "shows up in the report but does not flip ``passed`` to "
            "False. When true (only meaningful on reconciliation-grade "
            "templates that pin the IBKR fee model on both sides), "
            "COMMISSION_DRIFT joins the gating set. Same Branch-A "
            "semantics as the qc_reconciler."
        ),
    )


class CrossEngineFillSnapshotModel(BaseModel):
    """One side of a paired (LEAN, Engine Lab) divergence row.

    Carries enough information for the operator to understand WHICH fill
    was on this side without re-fetching the full normalized result. The
    Decimal-valued fields are wire-serialized as strings so the cents are
    exact (avoids float-binary loss in JSON, matches the Phase 5a
    convention).
    """

    symbol: str
    side: Literal["Buy", "Sell"]
    fill_quantity: int
    # Present only when ``fill_quantity`` was truncated from a fractional
    # value (LEAN can emit ``100.5``-style fills via fractional-share
    # algorithms). Mirrors the Phase 5a fee reconciler's
    # ``fill_quantity_raw`` convention. Wire type is string for
    # Decimal-exactness; ``None`` when ``fill_quantity`` already carries
    # the full precision.
    fill_quantity_raw: str | None = None
    fill_price: str
    fill_time_ms_utc: int
    fee: str | None = None


class CrossEngineDivergenceModel(BaseModel):
    """One typed disagreement between paired LEAN-Lab and Engine-Lab fills.

    Maps onto ``research.parity.qc_reconciler.Divergence``. When one side
    is missing (DECISION_MISMATCH), the corresponding snapshot is None.
    """

    category: CrossEngineDivergenceCategory
    trading_date: str = Field(
        ...,
        description=(
            "NY-local trading date in ISO YYYY-MM-DD form. The reconciler "
            "aligns on NY trading date so the wire form reflects that "
            "(extended-hours fills can have a UTC date one day off)."
        ),
    )
    detail: str
    lean_fill: CrossEngineFillSnapshotModel | None
    engine_fill: CrossEngineFillSnapshotModel | None


class CrossEngineReconciliationReportModel(BaseModel):
    """Phase 5g — cross-engine fill-by-fill reconciliation report.

    ``schema_version`` is the D10 contract: any future shape change bumps
    this so the consumer can fail-fast on an unrecognized version. The
    current shape is v1.
    """

    schema_version: int = Field(
        default=1,
        description=(
            "Explicit schema version per mission-critical doc D10. "
            "Consumers MUST fail-fast on an unrecognized version rather "
            "than silently misrender."
        ),
    )
    run_id: str
    engine_lab_strategy_class: str
    assert_fees: bool
    lean_total_fills: int
    engine_total_fills: int
    matched_count: int
    divergent_count: int
    # Subset of divergent_count: divergences in the gating set per
    # assert_fees + the default-strict policy. When this is 0 the
    # report has passed.
    gating_divergent_count: int
    passed: bool
    counts_by_category: dict[CrossEngineDivergenceCategory, int]
    divergences: list[CrossEngineDivergenceModel]


@router.post(
    "/runs/{run_id}/cross-reconcile",
    response_model=CrossEngineReconciliationReportModel,
    summary="Cross-engine reconciliation — diff this LEAN-Lab run against an Engine-Lab strategy.",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Caller-supplied strategy class is unknown / incompatible "
                "with the cross-run contract (must accept ``symbol`` "
                "kwarg), OR the LEAN-Lab manifest is missing fields the "
                "cross-runner needs (symbol, dates, starting cash)."
            ),
        },
        status.HTTP_404_NOT_FOUND: {
            "description": ("Run not found, or run completed but no normalized result.json / manifest.json on disk."),
        },
    },
)
async def post_cross_reconcile(
    run_id: str,
    payload: CrossReconcileRequestModel,
) -> CrossEngineReconciliationReportModel:
    """Phase 5g cross-engine reconciliation.

    Compares this LEAN-Lab run's fills against an Engine-Lab strategy
    run on the same staged workspace data (D3 — shared staged data,
    not Engine-Lab's native fixtures).

    Flow:

    1. Resolve LEAN-Lab workspace + load ``manifest.json`` (extract
       symbol, trading window, starting cash).
    2. Load LEAN's ``normalized/result.json`` (the Phase 3a parser
       output the orchestrator persisted).
    3. Run the caller-supplied Engine-Lab strategy class against the
       workspace data via
       :func:`cross_runner.run_engine_lab_on_workspace`.
    4. Diff via :func:`cross_reconciler.compare_cross_engine`. Default
       gating taxonomy is strict — every category gating EXCEPT
       ``COMMISSION_DRIFT`` (diagnostic). ``assert_fees=true`` (D3
       Branch-A) promotes ``COMMISSION_DRIFT`` to gating.
    5. Fold the comparator output into
       ``CrossEngineReconciliationReportModel`` (``schema_version=1``
       per D10).

    Error contract mirrors the Phase 5a self-reconciler where possible:

    * 404 ``run_not_found`` — invalid run_id, or workspace dir absent.
    * 404 ``normalized_missing`` — workspace exists but no parseable
      ``result.json`` (LEAN crashed before producing artifacts, or the
      file failed validation).
    * 404 ``manifest_missing`` — workspace exists but ``manifest.json``
      is absent (the orchestrator never finished writing it). The
      cross-run needs the manifest for symbol/dates/cash.
    * 400 ``manifest_incomplete`` — manifest present but missing one of
      the required fields (older manifest schema, or a malformed
      hand-edited file). Surfaces the missing field name in ``detail``.
    * 400 ``strategy_not_found`` — caller named an Engine-Lab strategy
      class that does not resolve. ``detail`` carries the known list.
    * 400 ``strategy_incompatible`` — strategy resolved but does not
      accept the ``symbol`` kwarg required by the Phase 5g.2 contract.
    """
    workspace = _resolved_workspace_or_404(run_id)
    result_path = workspace.normalized_dir / "result.json"
    if not result_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "normalized_missing",
                "message": (
                    f"cannot cross-reconcile {run_id}: normalized "
                    "result.json not present. The run may have crashed "
                    "before producing parseable output."
                ),
            },
        )
    try:
        normalized_result = NormalizedResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, NormalizedParserError) as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "normalized_missing",
                "message": (f"cannot cross-reconcile {run_id}: result.json failed to load ({e})."),
            },
        ) from e

    if not workspace.manifest_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "manifest_missing",
                "message": (
                    f"cannot cross-reconcile {run_id}: manifest.json not "
                    "present. The orchestrator did not finish recording "
                    "the run; cross-run inputs (symbol, dates, cash) "
                    "cannot be derived."
                ),
            },
        )
    try:
        manifest_data = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "manifest_missing",
                "message": (f"cannot cross-reconcile {run_id}: manifest.json failed to load ({e})."),
            },
        ) from e

    cross_inputs = _extract_cross_run_inputs_from_manifest(manifest_data, run_id=run_id)

    # Open-Q2 review-fix: ``assert_fees=true`` only makes sense when
    # both engines pin the same fee model. The Engine Lab side hard-
    # codes Phase 5a's ``IbkrEquityCommissionModel``; the LEAN side
    # only matches when the run used the Phase 5b reconciliation
    # template (which calls ``SetBrokerageModel(
    # BrokerageName.InteractiveBrokersBrokerage, ...)`` and records
    # ``brokerage_policy="interactive_brokers"`` in the manifest).
    # Promoting ``commission_drift`` to gating against a default-
    # brokerage LEAN run produces a deceptive "Failed" badge for an
    # inherently meaningless comparison; refuse at the boundary.
    if payload.assert_fees:
        brokerage_policy = manifest_data.get("brokerage_policy")
        if brokerage_policy != "interactive_brokers":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "assert_fees_requires_ibkr_brokerage",
                    "message": (
                        f"assert_fees=true requires the LEAN-Lab run to have "
                        "used the reconciliation template "
                        '(brokerage_policy="interactive_brokers"); '
                        f"got brokerage_policy={brokerage_policy!r}. Re-run "
                        "the LEAN-Lab side with template='reconciliation' "
                        "or call cross-reconcile with assert_fees=false."
                    ),
                    "manifest_brokerage_policy": brokerage_policy,
                },
            )

    try:
        cross_result = run_engine_lab_on_workspace(
            workspace.workspace_dir,
            payload.engine_lab_strategy_class,
            symbol=cross_inputs["symbol"],
            start_date=cross_inputs["start_date"],
            end_date=cross_inputs["end_date"],
            initial_cash=cross_inputs["initial_cash"],
        )
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "strategy_not_found",
                "message": str(e),
                "engine_lab_strategy_class": payload.engine_lab_strategy_class,
            },
        ) from e
    except StrategyIncompatibleError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "strategy_incompatible",
                "message": str(e),
                "engine_lab_strategy_class": payload.engine_lab_strategy_class,
            },
        ) from e
    except WorkspaceDataMissingError as e:
        # Workspace exists (we resolved it above) but the data/ subtree
        # is gone. Surface as 404 — a recoverable "needs restage" rather
        # than a server error.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "workspace_data_missing",
                "message": str(e),
            },
        ) from e

    comparator_output = compare_cross_engine(
        normalized_result.order_events,
        cross_result.order_events,
        assert_fees=payload.assert_fees,
    )

    return _build_cross_engine_report(
        comparator_output,
        run_id=run_id,
        engine_lab_strategy_class=payload.engine_lab_strategy_class,
        assert_fees=payload.assert_fees,
    )


def _extract_cross_run_inputs_from_manifest(manifest_data: dict, *, run_id: str) -> dict:
    """Pull symbol / start_date / end_date / initial_cash from a
    persisted manifest. Raises HTTPException(400) with
    ``reason: 'manifest_incomplete'`` and the offending field name when
    any required value is absent or malformed.

    The manifest schema has evolved across Phase 1 → 5: older runs may
    not have ``parameters.symbol`` (it was added when arbitrary-source
    runs landed). For those, the symbol falls back to the single key in
    ``bars_consumed_by_symbol`` if exactly one is present — that's a
    safe inference since the trusted sample is single-symbol.
    """
    parameters = manifest_data.get("parameters") or {}
    requested = manifest_data.get("requested_window_ms") or {}
    bars_consumed = manifest_data.get("bars_consumed_by_symbol") or {}

    # ---- Symbol ---------------------------------------------------------
    symbol_raw = parameters.get("symbol")
    if not symbol_raw:
        # Fallback for older single-symbol manifests.
        keys = list(bars_consumed.keys()) if isinstance(bars_consumed, dict) else []
        if len(keys) == 1:
            symbol_raw = keys[0]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "manifest_incomplete",
                    "message": (
                        f"cannot cross-reconcile {run_id}: manifest has no "
                        "``parameters.symbol`` and cannot infer one from "
                        "``bars_consumed_by_symbol``. Re-run the algorithm "
                        "with a current manifest schema."
                    ),
                    "missing_field": "parameters.symbol",
                },
            )
    symbol = str(symbol_raw).upper()

    # ---- Dates ----------------------------------------------------------
    start_str = parameters.get("start_date")
    end_str = parameters.get("end_date")
    if start_str and end_str:
        try:
            start_date = date.fromisoformat(start_str)
            end_date = date.fromisoformat(end_str)
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "manifest_incomplete",
                    "message": (
                        f"cannot cross-reconcile {run_id}: manifest "
                        f"parameters.start_date / end_date are not "
                        f"ISO date strings ({e})."
                    ),
                    "missing_field": "parameters.start_date|end_date",
                },
            ) from e
    else:
        # Fall back to requested_window_ms (always int64 ms UTC per
        # the manifest contract). Convert each end of the window to its
        # NY-local calendar date.
        start_ms = requested.get("start_ms") if isinstance(requested, dict) else None
        end_ms = requested.get("end_ms") if isinstance(requested, dict) else None
        if start_ms is None or end_ms is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "manifest_incomplete",
                    "message": (
                        f"cannot cross-reconcile {run_id}: manifest has "
                        "neither ``parameters.start_date/end_date`` nor "
                        "``requested_window_ms``."
                    ),
                    "missing_field": "parameters.start_date|requested_window_ms",
                },
            )
        try:
            start_date = datetime.fromtimestamp(int(start_ms) / 1000, tz=_NY_TIMEZONE_FOR_DATES).date()
            end_date = datetime.fromtimestamp(int(end_ms) / 1000, tz=_NY_TIMEZONE_FOR_DATES).date()
        except (TypeError, ValueError, OSError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "manifest_incomplete",
                    "message": (f"cannot cross-reconcile {run_id}: requested_window_ms is malformed ({e})."),
                    "missing_field": "requested_window_ms",
                },
            ) from e

    # ---- Starting cash --------------------------------------------------
    cash_raw = parameters.get("starting_cash") or manifest_data.get("starting_capital")
    if cash_raw is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "manifest_incomplete",
                "message": (
                    f"cannot cross-reconcile {run_id}: manifest has no "
                    "``parameters.starting_cash`` or top-level "
                    "``starting_capital``."
                ),
                "missing_field": "parameters.starting_cash",
            },
        )
    try:
        initial_cash = Decimal(str(cash_raw))
    except (TypeError, ValueError, InvalidDecimalOperation) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "manifest_incomplete",
                "message": (
                    f"cannot cross-reconcile {run_id}: starting_cash is not a valid number ({cash_raw!r}: {e})."
                ),
                "missing_field": "parameters.starting_cash",
            },
        ) from e

    return {
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": initial_cash,
    }


def _build_cross_engine_report(
    output: CrossReconciliationOutput,
    *,
    run_id: str,
    engine_lab_strategy_class: str,
    assert_fees: bool,
) -> CrossEngineReconciliationReportModel:
    """Convert the comparator's router-agnostic output to the wire model."""
    divergences = [
        CrossEngineDivergenceModel(
            category=d.category.value,  # type: ignore[arg-type]
            trading_date=d.trading_date.isoformat(),
            detail=d.detail,
            lean_fill=(
                CrossEngineFillSnapshotModel(**internal_fill_to_dict(d.lean_fill)) if d.lean_fill is not None else None
            ),
            engine_fill=(
                CrossEngineFillSnapshotModel(**internal_fill_to_dict(d.engine_fill))
                if d.engine_fill is not None
                else None
            ),
        )
        for d in output.divergences
    ]
    return CrossEngineReconciliationReportModel(
        run_id=run_id,
        engine_lab_strategy_class=engine_lab_strategy_class,
        assert_fees=assert_fees,
        lean_total_fills=output.lean_total_fills,
        engine_total_fills=output.engine_total_fills,
        matched_count=output.matched_count,
        divergent_count=output.divergent_count,
        gating_divergent_count=output.gating_divergent_count,
        passed=output.passed,
        counts_by_category={
            cat.value: n
            for cat, n in output.counts_by_category.items()  # type: ignore[misc]
        },
        divergences=divergences,
    )


@router.get(
    "/runs/{run_id}/log",
    response_class=PlainTextResponse,
    summary="Return LEAN's own log.txt (tail-capped) for a completed run.",
)
async def get_log(run_id: str) -> PlainTextResponse:
    workspace = _resolved_workspace_or_404(run_id)
    log_path = workspace.lean_log_path
    if not log_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "log_missing",
                "message": f"LEAN log.txt not present for {run_id}",
            },
        )
    # Read only the tail off disk so memory is bounded by the cap, not
    # by the underlying log size. ``read_text()`` would load the whole
    # file into memory before slicing — under concurrent requests on
    # a multi-GiB LEAN log that defeats the OOM protection the cap is
    # there to provide.
    size = log_path.stat().st_size
    with log_path.open("rb") as f:
        if size > _LEAN_LOG_TAIL_MAX_BYTES:
            f.seek(size - _LEAN_LOG_TAIL_MAX_BYTES)
        raw = f.read(_LEAN_LOG_TAIL_MAX_BYTES)
    return PlainTextResponse(raw.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# GET /diagnose — launcher reachability self-test
# ---------------------------------------------------------------------------


@router.get(
    "/diagnose",
    response_model=LauncherDiagnosticReport,
    summary="Self-test the data-plane → launcher path.",
)
async def diagnose_launcher() -> LauncherDiagnosticReport:
    """Layered self-test for the LEAN Sidecar launcher integration.

    Returns one row per check (URL configured, URL parses, token
    resolves, ``/healthz`` reachable) with a remediation hint when
    something is not passing. Read-only — sends only the launcher's
    unauthenticated ``/healthz`` probe; never spawns a sidecar run.

    Intended as the "did the data plane reach the launcher?" smoke
    test after a fresh clone, ``./restart.sh``, or a host-side
    relaunch of the launcher process.
    """
    return await run_launcher_diagnostics()


# ---------------------------------------------------------------------------
# POST /compare — trade-list divergence classifier (Task 3.2)
# ---------------------------------------------------------------------------


class _TradeRecordModel(BaseModel):
    """One closed round-trip trade in the persist-payload format.

    Financial fields use ``Decimal`` to avoid IEEE 754 rounding errors in
    cent-level divergence classification (FILL_PRICE_DRIFT / PNL_DRIFT).
    Pydantic v2 serialises Decimal from JSON numbers or strings transparently.
    """

    model_config = ConfigDict(frozen=True)

    trade_number: int
    entry_ms_utc: int
    exit_ms_utc: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    signal_reason: str
    is_synthetic_exit: bool = False
    # Optional brokerage fee — required when the caller sets assert_fees=True.
    # If absent, COMMISSION_DRIFT classification is silently skipped for this
    # trade (Branch B fixture semantics per numerical-rigor.md).
    fee: Decimal | None = None


class _CompareRequestModel(BaseModel):
    left_trades: list[_TradeRecordModel]
    right_trades: list[_TradeRecordModel]
    # Decimal tolerance: avoids float rounding errors at atol boundaries.
    fill_price_atol: Decimal = Field(default=Decimal("0.01"), ge=Decimal("0"))
    assert_fees: bool = False


class _DivergenceModel(BaseModel):
    category: str
    trade_number: int | None = None
    ms_utc: int | None = None
    message: str
    left_fill_price: Decimal | None = None
    right_fill_price: Decimal | None = None
    left_quantity: Decimal | None = None
    right_quantity: Decimal | None = None


class _CompareResponseModel(BaseModel):
    divergences: list[_DivergenceModel]
    first_divergence_ms_utc: int | None = None


@router.post(
    "/compare",
    response_model=_CompareResponseModel,
    summary="Classify divergences between two trade lists (Task 3.2).",
)
async def compare_trades(request: _CompareRequestModel) -> _CompareResponseModel:
    """Accept two trade arrays and return a classified divergence list.

    Pure compute — no DB access on the Python side. The .NET backend
    fetches trades from Postgres and POSTs them here (Task 3.3).
    """
    left = [t.model_dump() for t in request.left_trades]
    right = [t.model_dump() for t in request.right_trades]

    result: CompareResult = reconcile_trade_lists(
        left_trades=left,
        right_trades=right,
        fill_price_atol=request.fill_price_atol,
        assert_fees=request.assert_fees,
    )

    divergences = [
        _DivergenceModel(
            category=d.category,
            trade_number=d.trade_number,
            ms_utc=d.ms_utc,
            message=d.message,
            left_fill_price=d.left_fill_price,
            right_fill_price=d.right_fill_price,
            left_quantity=d.left_quantity,
            right_quantity=d.right_quantity,
        )
        for d in result.divergences
    ]

    return _CompareResponseModel(
        divergences=divergences,
        first_divergence_ms_utc=result.first_divergence_ms_utc,
    )
