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
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.lean_sidecar.config import DEFAULT_ARTIFACTS_ROOT, MAX_ALGORITHM_SOURCE_BYTES
from app.lean_sidecar.launcher_client import (
    LauncherClientError,
    LauncherRejected,
    LauncherUnreachable,
)
from app.lean_sidecar.normalized_parser import (
    NormalizedParserError,
    parse_workspace,
)
from app.lean_sidecar.reconciler import (
    DEFAULT_COMMISSION_ATOL,
    FeeReconciliationReport,
    reconcile_normalized_result,
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

    Phase 4c added the optional ``algorithm_source`` field — Phase 1c's
    mandatory sandbox shape (``--read-only``, ``--user=<non-root>``,
    ``--cap-drop=ALL``, ``--network=none``, workspace-only mount)
    closes the threat model that previously gated arbitrary user
    source. When omitted, the trusted ``buy_and_hold`` sample runs.

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
    template: Literal["trusted_default", "reconciliation"] = Field(
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
        return self


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
        algorithm_source=payload.algorithm_source,
        template=payload.template,
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
    kind = "unknown"
    for note in notes:
        if isinstance(note, str) and note.startswith("algorithm_source_kind="):
            value = note.split("=", 1)[1]
            if value in ("trusted_sample", "user_provided"):
                kind = value
            break
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
    }


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
    # Safety bound: hash the dir-list size and refuse to load manifests
    # past it. _RUN_INDEX_CAP is the display cap (what the UI shows);
    # _SCAN_HARD_CAP is the work cap (how many manifests we'll load
    # before sorting and truncating). 5× the display cap keeps the
    # work bounded but gives the sort enough headroom to pick the
    # truly-newest runs.
    scan_dirs = candidate_dirs[:_SCAN_HARD_CAP] if len(candidate_dirs) > _SCAN_HARD_CAP else candidate_dirs
    # Iterate in run_id-desc order so the safety bound preferentially
    # keeps the lexically-newest candidates (which for modern slug-
    # prefixed-by-timestamp IDs approximates newest-first).
    scan_dirs.sort(key=lambda p: p.name, reverse=True)
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
    # ``None`` when ``category == "no_recorded_fee"`` — there's nothing
    # to subtract from. Non-null for every ``commission_drift`` row.
    delta: str | None
    category: Literal["commission_drift", "no_recorded_fee"]


class RunReconciliationReportModel(BaseModel):
    """Phase 5a — categorized fee-divergence report for one LEAN Lab run.

    The report is decoupled from whether the run was reconciliation-grade.
    A default-brokerage trusted-sample run will naturally surface many
    ``commission_drift`` rows because LEAN's default commission differs
    from IBKR's tier — that signal is informative, not a bug.
    """

    run_id: str
    algorithm_id: str
    total_fill_events: int
    matched_count: int
    divergent_count: int
    # All Decimals on the wire are strings — preserves exact cents and
    # documents the tolerance regime in numerical-rigor.md.
    commission_atol: str
    total_recorded_fees: str
    total_expected_ibkr_fees: str
    divergences: list[FeeDivergenceModel]


def _report_to_model(report: FeeReconciliationReport, algorithm_id: str) -> RunReconciliationReportModel:
    return RunReconciliationReportModel(
        run_id=report.run_id,
        algorithm_id=algorithm_id,
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
    """
    workspace = _resolved_workspace_or_404(run_id)
    try:
        result = parse_workspace(workspace)
    except NormalizedParserError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "normalized_missing",
                "message": (
                    f"cannot reconcile {run_id}: normalized result not present "
                    f"({e}). The run may have crashed before producing parseable output."
                ),
            },
        ) from e
    report = reconcile_normalized_result(result, commission_atol=DEFAULT_COMMISSION_ATOL)
    return _report_to_model(report, algorithm_id=result.algorithm_id)


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
