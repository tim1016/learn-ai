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
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.lean_sidecar.config import DEFAULT_ARTIFACTS_ROOT
from app.lean_sidecar.launcher_client import (
    LauncherClientError,
    LauncherRejected,
    LauncherUnreachable,
)
from app.lean_sidecar.workspace import (
    RUN_ID_PATTERN,
    TICKER_SYMBOL_PATTERN,
    SymbolValidationError,
    WorkspaceError,
    resolve_workspace,
    validate_symbol,
)
from app.services.lean_sidecar_service import (
    LeanSidecarServiceError,
    TrustedRunRequest,
    run_trusted_sample,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Phase 2a caps for caller-supplied inputs. These are smaller than the
# launcher's own ceilings so a bad input is rejected here with a
# meaningful 422 before any container work.
_MAX_TRADING_DAYS = 30
_MAX_STARTING_CASH = 10_000_000.0
_MIN_STARTING_CASH = 1_000.0

# Window inputs are int64 ms UTC per ``.claude/rules/numerical-rigor.md``
# §"Timestamp rigor". Trading-day semantics live below this boundary
# (the orchestrator resolves the ms range into trading dates after
# converting to ET).
_MIN_EPOCH_MS = 1_000_000_000_000  # 2001-09-09 — well before any LEAN data we'd run
_MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01 — far future sanity bound


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


class TrustedRunRequestModel(BaseModel):
    """Pydantic shape for POST /api/lean-sidecar/trusted-runs.

    There is intentionally **no** ``algorithm_source`` field — Phase 3
    is the gating phase before any user-authored source crosses the
    launcher boundary. Reviewers / fuzzers adding such a field here
    must update the ADR row in the same PR.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        ...,
        pattern=RUN_ID_PATTERN.pattern,
        description="Slug matching ^[a-z0-9][a-z0-9_-]{2,63}$",
    )
    symbol: str = Field(
        default="SPY",
        pattern=TICKER_SYMBOL_PATTERN.pattern,
        description=(
            "Equity ticker. Must match the strict ticker regex so it cannot "
            "smuggle path separators into the LEAN data-folder layout; the "
            "service layer re-validates as defense-in-depth."
        ),
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

    @model_validator(mode="after")
    def _validate_window(self) -> TrustedRunRequestModel:
        if self.end_ms_utc <= self.start_ms_utc:
            raise ValueError("end_ms_utc must be strictly greater than start_ms_utc")
        # Pre-launcher cap on date range — the Phase 2a synthetic-bar
        # generator and the launcher's wall-clock timeout both scale
        # with the window. Count *trading days* (weekdays), not
        # calendar days: a window with 30 weekdays plus surrounding
        # weekends should pass even if it's 40 calendar days wide.
        trading_days = _count_weekdays_between(self.start_ms_utc, self.end_ms_utc)
        if trading_days > _MAX_TRADING_DAYS:
            raise ValueError(f"window spans {trading_days} trading days; max is {_MAX_TRADING_DAYS}")
        if trading_days == 0:
            raise ValueError("window contains no weekdays — staging would produce zero bars")
        # Symbol must pass the full validator — the field-level
        # regex catches ``/``, ``\``, length, and the alphabet, but
        # not the dot-only case (``"."``, ``".."``). validate_symbol
        # closes that hole and is the same function the staging
        # writers re-check against.
        try:
            validate_symbol(self.symbol)
        except SymbolValidationError as e:
            raise ValueError(str(e)) from e
        return self


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
    request = TrustedRunRequest(
        run_id=payload.run_id,
        symbol=payload.symbol.upper(),
        start_ms_utc=payload.start_ms_utc,
        end_ms_utc=payload.end_ms_utc,
        starting_cash=payload.starting_cash,
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
    )


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
