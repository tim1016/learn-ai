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

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.lean_sidecar.config import MAX_ALGORITHM_SOURCE_BYTES
from app.lean_sidecar.diagnostics import (
    LauncherDiagnosticReport,
    run_launcher_diagnostics,
)
from app.lean_sidecar.launcher_client import (
    LauncherClientError,
    LauncherRejected,
    LauncherUnreachable,
)
from app.lean_sidecar.staging import MetadataStagingError
from app.lean_sidecar.trading_calendar import (
    is_trading_day,
    next_trading_day,
    session_open_ms_utc,
)
from app.lean_sidecar.trusted_templates import TrustedTemplate
from app.lean_sidecar.workspace import (
    RUN_ID_PATTERN,
    TICKER_SYMBOL_PATTERN,
    SymbolValidationError,
    validate_symbol,
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

# Window inputs are int64 ms UTC. Trading-day semantics live below this boundary
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
            f"trading day), the ADR 0022 trading-date anchor; got {dt_et.isoformat()}."
        )
    return dt_et.date()


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


class EmaCrossover2BpsStrategyParametersModel(BaseModel):
    """Validated runtime gates accepted by the parameterized LEAN twin."""

    model_config = ConfigDict(extra="forbid")

    gap_bps: float = Field(2.0, ge=0.0, le=100.0, allow_inf_nan=False)
    rsi_min: float = Field(50.0, ge=0.0, le=100.0, allow_inf_nan=False)
    rsi_max: float = Field(70.0, ge=0.0, le=100.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_rsi_band(self) -> EmaCrossover2BpsStrategyParametersModel:
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be less than rsi_max")
        return self


class EmaCrossoverSignalStrategyParametersModel(BaseModel):
    """All four resolved entry gates accepted by the canonical EMA twin."""

    model_config = ConfigDict(extra="forbid")

    gap: float = Field(0.20, ge=0.0, allow_inf_nan=False)
    gap_bps: float = Field(0.0, ge=0.0, le=100.0, allow_inf_nan=False)
    rsi_min: float = Field(50.0, ge=0.0, le=100.0, allow_inf_nan=False)
    rsi_max: float = Field(70.0, ge=0.0, le=100.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_rsi_band(self) -> EmaCrossoverSignalStrategyParametersModel:
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be less than rsi_max")
        return self


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

    requested_engine: Literal["lean", "both"] = Field(
        "lean",
        description="Operator-selected execution mode, persisted for exact History rehydration.",
    )

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
            "Every wire timestamp "
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
    template: TrustedTemplate = Field(
        default=TrustedTemplate.TRUSTED_DEFAULT,
        description=(
            "Phase 5b — which bundled trusted sample to stage when "
            "``algorithm_source`` is omitted. ``trusted_default`` (the "
            "back-compat default) runs the LEAN-default-brokerage "
            "sample; ``reconciliation`` runs the IBKR-brokerage-pinned "
            "sample that the Phase 5a fee reconciler returns a clean "
            "report for. Ignored when ``algorithm_source`` is provided."
        ),
    )
    strategy_parameters: EmaCrossover2BpsStrategyParametersModel | EmaCrossoverSignalStrategyParametersModel | None = Field(
        default=None,
        description=(
            "Validated strategy-logic parameters for a bundled trusted template. "
            "Accepted by the parameterized EMA twins; omitted values use that "
            "template's declared defaults."
        ),
    )

    @field_validator("strategy_parameters", mode="before")
    @classmethod
    def _select_strategy_parameter_schema(cls, value: object, info: ValidationInfo) -> object:
        """Apply defaults only after the template has selected its schema.

        Both EMA models accept a partial object. Letting the union choose first
        would therefore inject the two-bps defaults before the signal template
        could select its own zero-bps default.
        """
        if value is None:
            return None
        raw = value.model_dump(exclude_unset=True) if isinstance(value, BaseModel) else value
        template = info.data.get("template")
        if template == TrustedTemplate.EMA_CROSSOVER_2_BPS:
            return EmaCrossover2BpsStrategyParametersModel.model_validate(raw)
        if template == TrustedTemplate.EMA_CROSSOVER_SIGNAL:
            return EmaCrossoverSignalStrategyParametersModel.model_validate(raw)
        return raw

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
        if self.algorithm_source is not None:
            if self.strategy_parameters is not None:
                raise ValueError("algorithm_source runs do not accept strategy_parameters")
        elif self.template == TrustedTemplate.EMA_CROSSOVER_2_BPS:
            parameters = EmaCrossover2BpsStrategyParametersModel.model_validate(
                {} if self.strategy_parameters is None else self.strategy_parameters.model_dump()
            )
            object.__setattr__(self, "strategy_parameters", parameters)
        elif self.template == TrustedTemplate.EMA_CROSSOVER_SIGNAL:
            parameters = EmaCrossoverSignalStrategyParametersModel.model_validate(
                {} if self.strategy_parameters is None else self.strategy_parameters.model_dump()
            )
            object.__setattr__(self, "strategy_parameters", parameters)
        elif self.strategy_parameters is not None:
            raise ValueError(f"template {self.template.value} does not accept strategy_parameters")
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
    # The typed save outcome (#2464): ``saved``, ``failed``, or ``unknown``
    # (the write may still commit — never present that as "not saved").
    save_outcome: Literal["saved", "failed", "unknown"] = "failed"


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
        requested_engine=payload.requested_engine,
        parity_group_id=payload.parity_group_id,
        strategy_parameters=(
            tuple(payload.strategy_parameters.model_dump().items())
            if payload.strategy_parameters is not None
            else ()
        ),
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
        save_outcome=result.save_outcome,
    )


# ---------------------------------------------------------------------------
# Run-history index
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inspection endpoints
# ---------------------------------------------------------------------------
#
# These read artifacts from an existing run's workspace. They never
# touch the launcher; they only serve files the launcher (or the
# orchestrator) already wrote.


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


# ---------------------------------------------------------------------------
# Phase 5g — cross-engine reconciliation scaffold
# ---------------------------------------------------------------------------


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
