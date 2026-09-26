"""Broker-v2 bot control panel routes (transport only).

``/api/brokers/{broker}/...`` — the panel contract surface (spec §3-§8, §11).
The router validates/parses the HTTP request, calls the panel data-source
facade, and translates typed panel errors to HTTP. No business logic lives here
(router-freeze discipline).

Account scope (§3): read/projection/action endpoints are account-scoped
(``/accounts/{account_id}/...``) and validate ``account_id`` against the
broker's account (mismatch → 404). The unscoped forms are kept as aliases for
the single-account case — no breaking rename.

Identity (§14): control mutations authenticate via the always-on data-plane
control secret (the router prefix carries it); the server attaches the
configured ``PANEL_OPERATOR_IDENTITY`` — operator identity is never a request
field.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Literal, NoReturn

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.broker.contract.models import US_EQUITY_SYMBOL_PATTERN
from app.config import settings
from app.schemas.broker_bots import (
    AlpacaPaperDeployReceipt,
    AlpacaPaperDeployRequest,
    AlpacaPaperDeployView,
    BotControlAuthorityFacts,
)
from app.schemas.broker_v2_evidence import EvidencePage
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelLiveSnapshot,
    BotPanelView,
    ChartHistoryResponse,
    ChartHistoryTimeframe,
    ChartLiveResponse,
    CohortActionResult,
    CohortArchiveRequest,
    CohortArchiveView,
    CohortFlattenRequest,
    CohortFlattenView,
    LiveSnapshotUnavailableDetail,
    LiveSnapshotUnavailableResponse,
    PanelAction,
    PanelActionErrorResponse,
    PanelActionRequest,
    PanelActionResult,
    PanelProfile,
    PanelQuiesceActionRequest,
)
from app.schemas.canary_admission import (
    CanaryActivationConfirmation,
    CanaryActivationPlan,
    CanaryActivationRequest,
    CanaryAdmissionEvent,
)
from app.schemas.exit_terms import ExitTermsInput
from app.schemas.run_admission import RunAdmissionDecision
from app.services.broker_v2_panel import (
    cohort_archive,
    cohort_flatten,
    panel_deploy,
    panel_errors,
    panel_scope,
)
from app.services.broker_v2_panel import panel_data_source as ds
from app.services.broker_v2_panel.action_execution_service import (
    ActionExecutionError,
    ActionOutcomeUnknownError,
    DryRunAuthorityLeaseLostError,
    ExecutionAuthorityRevivedError,
    StaleRevisionError,
)
from app.services.broker_v2_panel.chart_projection_service import (
    ChartTimeframeError,
    coerce_history_timeframe,
)
from app.services.broker_v2_panel.evidence_service import (
    PAGE_SIZE_DEFAULT,
    read_evidence_page,
)
from app.services.broker_v2_panel.live_projection import (
    get_or_start_live_projection_hub,
    release_live_projection_hub,
    retain_live_projection_hub,
    schedule_live_projection_refresh,
)
from app.services.broker_v2_panel.panel_profile_service import panel_profile_for
from app.services.broker_v2_panel.paper_access_service import (
    confirm_paper_access,
    prepare_paper_access,
)
from app.services.canary_admission import CanaryActivationRefused, CanaryAdmissionLedgerError
from app.services.surface_hub import (
    SnapshotStalledError,
    SnapshotUnavailableError,
    SurfaceHub,
    SurfaceHubRefreshFailure,
    SurfaceHubStall,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["broker-v2-panel"])


def _raise_panel_error(error: panel_errors.PanelDataError) -> NoReturn:
    raise HTTPException(
        status_code=error.http_status,
        detail={
            "message": str(error),
            "why": error.detail,
            "next_action": error.next_action,
        },
    )


def _raise_alpaca_deploy_error(error: panel_errors.PanelDataError) -> NoReturn:
    """Typed-conflict body shared by admission preview and actual deploy.

    Both endpoints preflight through the same
    ``ds._require_alpaca_deploy_request`` and must refuse a non-selectable
    strategy (and every other preflight conflict) with an identical body
    shape. No receipt exists on any error path, so ``receipt_id`` is always
    ``None`` here.
    """
    operation_attempted = isinstance(error, panel_errors.PanelRunnerError) and error.operation_attempted
    outcome = (
        "conflict"
        if error.http_status == 409
        else ("unknown" if operation_attempted and error.http_status >= 500 else "blocked")
    )
    raise HTTPException(
        status_code=error.http_status,
        detail={
            "outcome": outcome,
            "receipt_id": None,
            "recorded_at_ms": now_ms_utc(),
            "message": str(error),
            "why": error.detail,
            "next_action": error.next_action,
            "admission": (
                error.admission_decision.model_dump(mode="json")
                if isinstance(error, panel_errors.PanelRunnerError) and error.admission_decision is not None
                else None
            ),
            # A lane-level start gate (the go-live hold, #2269) has no
            # admission decision to carry its code in.
            "reason_code": (
                error.reason_code if isinstance(error, panel_errors.PanelRunnerError) else None
            ),
        },
    ) from error


def _raise_paper_access_error(
    error: CanaryActivationRefused | CanaryAdmissionLedgerError,
) -> NoReturn:
    raise HTTPException(
        status_code=409,
        detail={
            "message": "Paper access could not be changed.",
            "why": str(error),
            "next_action": "Prepare a fresh review after resolving the named blocker.",
        },
    ) from error


def _raise_action_error(error: ActionExecutionError, request: PanelActionRequest) -> NoReturn:
    outcome_unknown = isinstance(error, ActionOutcomeUnknownError)
    # ExecutionAuthorityRevivedError shares StaleRevisionError's retryable
    # reading, not a hard failure's: nothing applied and a same-key re-POST
    # (a fresh key for a solo action; the same derived key for a cohort leg,
    # per action_execution_service's release-not-fail contract) covers it --
    # exactly the "conflict" vocabulary already used for a stale token.
    # cohort_execution.py's per-leg classification maps this error to
    # "refused" -> "conflict" for the identical reason; aligned here so both
    # surfaces report the same outcome for the same condition (#1955 final
    # review). DryRunAuthorityLeaseLostError is retryable for the same
    # reason: nothing applied, and the bot's own synthetic heartbeat revives
    # its lease after a thaw.
    outcome: Literal["conflict", "failure", "unknown"] = (
        "unknown"
        if outcome_unknown
        else (
            "conflict"
            if isinstance(
                error,
                (StaleRevisionError, ExecutionAuthorityRevivedError, DryRunAuthorityLeaseLostError),
            )
            else "failure"
        )
    )
    raise HTTPException(
        status_code=error.http_status,
        detail=PanelActionErrorResponse(
            action_id=request.action_id,
            outcome=outcome,
            receipt_id=request.idempotency_key if outcome_unknown else None,
            recorded_at_ms=now_ms_utc(),
            message=str(error),
            why=error.detail,
            reason_code=error.reason_code,
        ).model_dump(mode="json"),
    )


# ── §4 Panel capability profile (broker-level) ───────────────────────────────


@router.get(
    "/{broker}/panel-profile",
    response_model=PanelProfile,
    summary="Closed panel capability profile for this broker (§4)",
)
async def get_panel_profile(broker: str) -> PanelProfile:
    profile = panel_profile_for(broker)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Broker '{broker}' has no panel capability profile.",
                "why": "Only Alpaca exposes the broker-v2 panel in phase 1.",
            },
        )
    return profile


# ── §5 Catalog (account-scoped + unscoped alias) ─────────────────────────────


async def _catalog(broker: str, account_id: str) -> list[BotCatalogView]:
    try:
        return await ds.get_catalog(broker, account_id)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/catalog",
    response_model=list[BotCatalogView],
    summary="Bots-list roster: status + slice-0 rollups (§5)",
)
async def get_catalog_scoped(broker: str, account_id: str) -> list[BotCatalogView]:
    return await _catalog(broker, account_id)


@router.get(
    "/{broker}/bots/catalog",
    response_model=list[BotCatalogView],
    summary="Bots-list roster (single-account alias of the scoped route) (§5)",
)
async def get_catalog_unscoped(broker: str) -> list[BotCatalogView]:
    account_id = await _resolve_default_account(broker)
    return await _catalog(broker, account_id)


# ── §5 Deploy (account-scoped alias of the bot-runner deploy route) ──────────


@router.get(
    "/{broker}/accounts/{account_id}/bots/deploy",
    response_model=AlpacaPaperDeployView,
    summary="Backend-authored Alpaca paper deployment contract",
)
async def get_alpaca_paper_deploy_view(
    broker: str,
    account_id: str,
    symbol: str | None = Query(
        None,
        # The canonical US-equity grammar the broker contract already enforces.
        # A length bound alone would let a malformed symbol reach the clerk and
        # fail somewhere less legible than the transport boundary.
        pattern=US_EQUITY_SYMBOL_PATTERN,
        description=(
            "Scope the channel-health verdict to one symbol. Omitted, the view "
            "reports account-level channel presence and connectivity only."
        ),
    ),
    exit_allowance_bps: float | None = Query(None),
    band_multiple: float | None = Query(None),
    spread_cap_bps: float | None = Query(None),
) -> AlpacaPaperDeployView:
    supplied = {name: value for name, value in {
        "exit_allowance_bps": exit_allowance_bps, "band_multiple": band_multiple, "spread_cap_bps": spread_cap_bps,
    }.items() if value is not None}
    try:
        terms = ExitTermsInput.model_validate(supplied) if supplied else None
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False)) from exc
    try:
        return await panel_deploy.get_alpaca_paper_deploy_view(broker, account_id, symbol, terms)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


@router.post(
    "/{broker}/accounts/{account_id}/strategies/{program_key}/paper-access/plan",
    response_model=CanaryActivationPlan,
    summary="Prepare a proof-bound review for one strategy/account Paper pairing",
)
async def prepare_strategy_paper_access(
    broker: str,
    account_id: str,
    program_key: str,
    request: CanaryActivationRequest,
) -> CanaryActivationPlan:
    try:
        return await prepare_paper_access(
            broker=broker,
            account_id=account_id,
            program_key=program_key,
            actor=settings.PANEL_OPERATOR_IDENTITY,
            reason=request.reason,
        )
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)
    except (CanaryActivationRefused, CanaryAdmissionLedgerError) as error:
        _raise_paper_access_error(error)


@router.post(
    "/{broker}/accounts/{account_id}/strategies/{program_key}/paper-access/confirm",
    response_model=CanaryAdmissionEvent,
    status_code=201,
    summary="Confirm the exact reviewed strategy/account Paper pairing",
)
async def confirm_strategy_paper_access(
    broker: str,
    account_id: str,
    program_key: str,
    request: CanaryActivationConfirmation,
) -> CanaryAdmissionEvent:
    try:
        return await confirm_paper_access(
            broker=broker,
            account_id=account_id,
            program_key=program_key,
            plan=request.plan,
            confirmation_token=request.confirmation_token,
        )
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)
    except (CanaryActivationRefused, CanaryAdmissionLedgerError) as error:
        _raise_paper_access_error(error)


@router.post(
    "/{broker}/accounts/{account_id}/bots/admission",
    response_model=RunAdmissionDecision,
    summary="Preview the exact Start admission used by execution",
)
async def preview_bot_start_admission_scoped(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
) -> RunAdmissionDecision:
    try:
        return await panel_deploy.preview_alpaca_paper_start_admission(
            broker,
            account_id,
            request,
        )
    except panel_errors.PanelDataError as error:
        _raise_alpaca_deploy_error(error)


@router.post(
    "/{broker}/accounts/{account_id}/bots",
    response_model=AlpacaPaperDeployReceipt,
    status_code=201,
    summary="Deploy one Clerk-governed Alpaca paper bot (§5)",
)
async def deploy_bot_scoped(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
) -> AlpacaPaperDeployReceipt:
    try:
        return await panel_deploy.deploy_alpaca_paper_bot(broker, account_id, request)
    except panel_errors.PanelDataError as error:
        _raise_alpaca_deploy_error(error)


# ── §7 Panel projection (account-scoped + unscoped alias) ────────────────────


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/authority-facts",
    response_model=BotControlAuthorityFacts,
    summary="Independent process and Clerk custody facts for one bot",
)
async def get_authority_facts_scoped(
    broker: str,
    account_id: str,
    sid: str,
) -> BotControlAuthorityFacts:
    try:
        return await ds.get_authority_facts(broker, account_id, sid)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


async def _panel(broker: str, account_id: str, sid: str, transaction_ref: str | None) -> BotPanelView:
    try:
        return await ds.get_panel(broker, account_id, sid, transaction_ref=transaction_ref)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/panel",
    response_model=BotPanelView,
    summary="5s-poll panel projection for one bot (§7)",
)
async def get_panel_scoped(
    broker: str,
    account_id: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
) -> BotPanelView:
    return await _panel(broker, account_id, sid, transaction_ref)


@router.get(
    "/{broker}/bots/{sid}/panel",
    response_model=BotPanelView,
    summary="Panel projection (single-account alias) (§7)",
)
async def get_panel_unscoped(
    broker: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
) -> BotPanelView:
    account_id = await _resolve_default_account(broker)
    return await _panel(broker, account_id, sid, transaction_ref)


_LIVE_STREAM_KEEPALIVE_S = 15.0


def _stall_detail(stall: SurfaceHubStall) -> LiveSnapshotUnavailableDetail:
    """Operator copy for a producer that stopped completing refreshes (#2353)."""
    return LiveSnapshotUnavailableDetail(
        reason="PRODUCER_STALLED",
        message="The live panel stopped updating.",
        why=(
            "The data plane has not completed a panel refresh in over "
            f"{stall.stall_after_ms // 1_000} seconds, so the last snapshot may no "
            "longer match the bot, its orders or the market."
        ),
        next_action=(
            "The values shown are frozen; the controls still work and each action "
            "is checked by the server. The panel resumes on its own when the "
            "producer recovers; if it does not, check the data plane."
        ),
        last_produced_at_ms=stall.last_produced_at_ms,
        observed_at_ms=stall.observed_at_ms,
    )


def _snapshot_unavailable(error: SnapshotUnavailableError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=LiveSnapshotUnavailableDetail(
            reason="SNAPSHOT_UNAVAILABLE",
            message="The live panel snapshot is not available yet.",
            why=str(error),
            next_action="Keep the current screen visible while the producer retries.",
            last_produced_at_ms=None,
            observed_at_ms=None,
        ).model_dump(mode="json"),
    )


async def _live_hub(
    broker: str,
    account_id: str,
    sid: str,
    resolution: Literal["5s", "1m"],
) -> SurfaceHub[BotPanelLiveSnapshot]:
    try:
        await panel_scope.validate_account_scope(broker, account_id, sid)
        return await get_or_start_live_projection_hub(
            broker,
            account_id,
            sid,
            resolution=resolution,
        )
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


async def _live_snapshot(
    broker: str,
    account_id: str,
    sid: str,
    resolution: Literal["5s", "1m"],
) -> BotPanelLiveSnapshot:
    hub = await _live_hub(broker, account_id, sid, resolution)
    try:
        return await hub.snapshot()
    except SnapshotStalledError as error:
        raise HTTPException(
            status_code=503,
            detail=_stall_detail(error.stall).model_dump(mode="json"),
        ) from error
    except SnapshotUnavailableError as error:
        raise _snapshot_unavailable(error) from error


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/live-snapshot",
    response_model=BotPanelLiveSnapshot,
    summary="Versioned REST bootstrap for the live bot panel",
    responses={
        503: {
            "model": LiveSnapshotUnavailableResponse,
            "description": (
                "The producer has not published a complete snapshot, its last "
                "refresh failed, or it stalled (reason PRODUCER_STALLED)."
            ),
        },
    },
)
async def get_live_snapshot_scoped(
    broker: str,
    account_id: str,
    sid: str,
    resolution: Literal["5s", "1m"] = Query("5s"),
) -> BotPanelLiveSnapshot:
    return await _live_snapshot(broker, account_id, sid, resolution)


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/live-stream",
    summary="Latest-wins SSE stream of complete bot-panel snapshots",
    responses={
        503: {
            "model": LiveSnapshotUnavailableResponse,
            "description": (
                "The producer has not published a complete snapshot, or its last "
                "refresh failed. A stalled producer still opens the stream, which "
                "reports the stall as a typed `stale` event."
            ),
        },
    },
)
async def stream_live_snapshot_scoped(
    broker: str,
    account_id: str,
    sid: str,
    resolution: Literal["5s", "1m"] = Query("5s"),
    cursor: str | None = Query(default=None, max_length=128),
) -> StreamingResponse:
    # A stalled producer still opens the stream: the stream itself reports the
    # stall as a typed ``stale`` event. A client that does not hold the frozen
    # frame gets it first, so a cold load still mounts the panel's controls.
    hub = await _live_hub(broker, account_id, sid, resolution)
    if not hub.is_available:
        raise _snapshot_unavailable(SnapshotUnavailableError(hub.strategy_instance_id))
    stream_epoch = hub.stream_epoch
    current_id = f"{stream_epoch}:{hub.surface_version}"
    requested_epoch = cursor.rsplit(":", 1)[0] if cursor and ":" in cursor else None

    async def event_source() -> AsyncIterator[str]:
        queue = hub.subscribe()
        retain_live_projection_hub(
            broker,
            account_id,
            sid,
            resolution=resolution,
        )
        try:
            if cursor is not None and requested_epoch != stream_epoch:
                payload = json.dumps({"reason": "epoch_changed", "cursor": current_id})
                yield f"event: reset\ndata: {payload}\n\n"
            stale_announced = False
            while True:
                # Wake no later than the stall deadline (at once if it already
                # passed unannounced), so staleness is judged by the producer's
                # own stamp, never by transport keepalives.
                until_stall = hub.seconds_until_stall()
                timeout = (
                    _LIVE_STREAM_KEEPALIVE_S
                    if until_stall is None or stale_announced
                    else min(_LIVE_STREAM_KEEPALIVE_S, until_stall)
                )
                try:
                    snapshot = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    stall = hub.stall()
                    stale_announced = stall is not None
                    yield _live_stream_frame(stall, None)
                    continue
                if snapshot is None:
                    yield "event: end\ndata: {}\n\n"
                    return
                if isinstance(snapshot, SurfaceHubRefreshFailure):
                    payload = json.dumps({"error": snapshot.message})
                    yield f"event: error\ndata: {payload}\n\n"
                    return
                stall = hub.stall()
                stale_announced = stall is not None
                if stall is not None and _live_frame_id(snapshot) != cursor:
                    # The frozen frame a stalled hub primed this subscriber
                    # with: an explicitly stale bootstrap, followed at once by
                    # the stall, so a cold client mounts the controls (#2353).
                    yield _live_stream_frame(None, snapshot)
                yield _live_stream_frame(stall, snapshot)
        finally:
            hub.unsubscribe(queue)
            release_live_projection_hub(
                broker,
                account_id,
                sid,
                resolution=resolution,
                hub=hub,
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _live_frame_id(snapshot: BotPanelLiveSnapshot) -> str:
    return f"{snapshot.stream_epoch}:{snapshot.surface_version}"


def _live_stream_frame(
    stall: SurfaceHubStall | None,
    snapshot: BotPanelLiveSnapshot | None,
) -> str:
    """The next live-stream frame; a stall outranks the snapshot and the keepalive."""
    if stall is not None:
        return f"event: stale\ndata: {_stall_detail(stall).model_dump_json()}\n\n"
    if snapshot is None:
        return ": keepalive\n\n"
    return (
        f"id: {_live_frame_id(snapshot)}\nevent: snapshot\n"
        f"data: {snapshot.model_dump_json()}\n\n"
    )


# ── §11 Presented-action execution (account-scoped + unscoped alias) ─────────


async def _run_action(broker: str, account_id: str, sid: str, request: PanelActionRequest) -> PanelActionResult:
    try:
        result = await ds.run_action(
            broker,
            account_id,
            sid,
            request,
            operator_identity=settings.PANEL_OPERATOR_IDENTITY,
        )
        schedule_live_projection_refresh(broker, account_id, sid)
        return result
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)
    except ActionExecutionError as error:
        _raise_action_error(error, request)


_ACTION_ERROR_RESPONSES = {
    409: {
        "model": PanelActionErrorResponse,
        "description": "The action's revision/concurrency token is stale, or it is no longer available.",
    },
    500: {
        "model": PanelActionErrorResponse,
        "description": "The performer began but did not return a terminal command receipt.",
    },
}


@router.post(
    "/{broker}/accounts/{account_id}/bots/{sid}/actions",
    response_model=PanelActionResult,
    summary="Execute one presented action (revision-guarded, idempotent) (§11)",
    responses=_ACTION_ERROR_RESPONSES,
)
async def run_action_scoped(broker: str, account_id: str, sid: str, request: PanelActionRequest) -> PanelActionResult:
    return await _run_action(broker, account_id, sid, request)


@router.post(
    "/{broker}/accounts/{account_id}/bots/{sid}/actions/quiesce",
    response_model=PanelActionResult,
    summary="Execute one presented quiesce action: stop, flatten or reconcile (§11, #2351)",
    responses=_ACTION_ERROR_RESPONSES,
)
async def run_quiesce_action_scoped(
    broker: str, account_id: str, sid: str, request: PanelQuiesceActionRequest
) -> PanelActionResult:
    """The panel's quiesce actions, on an operation of their own.

    A draining lane routes this operation and refuses ``/actions`` (#2351,
    ADR 0063 §2): the request schema admits only the actions that stop a bot,
    reduce its exposure or reconcile, and the execution is the one every
    panel action shares.
    """
    return await _run_action(broker, account_id, sid, request)


# ── §11b Cohort flatten (ADR 0051, #1802) ────────────────────────────────────


@router.get(
    "/{broker}/accounts/{account_id}/bots/cohort-flatten",
    response_model=CohortFlattenView,
    summary="Cohort-flatten presentation: (strategy, symbol) groups with per-leg executability facts (ADR 0051)",
)
async def get_cohort_flatten_scoped(broker: str, account_id: str) -> CohortFlattenView:
    try:
        return await cohort_flatten.get_cohort_flatten_view(broker, account_id)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


@router.post(
    "/{broker}/accounts/{account_id}/bots/cohort-flatten",
    response_model=CohortActionResult,
    summary="Execute a batch of per-bot flatten legs with per-leg receipts (ADR 0051)",
)
async def run_cohort_flatten_scoped(
    broker: str, account_id: str, request: CohortFlattenRequest
) -> CohortActionResult:
    try:
        result = await cohort_flatten.run_cohort_flatten(
            broker,
            account_id,
            request,
            operator_identity=settings.PANEL_OPERATOR_IDENTITY,
        )
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)
    for leg in result.legs:
        if leg.outcome in ("applied", "replayed"):
            schedule_live_projection_refresh(broker, account_id, leg.strategy_instance_id)
    return result


# ── §11c Cohort archive (ADR 0052, #1911) ────────────────────────────────────


@router.get(
    "/{broker}/accounts/{account_id}/bots/cohort-archive",
    response_model=CohortArchiveView,
    summary="Cohort-archive presentation: archivable bots grouped with per-leg executability facts (ADR 0052)",
)
async def get_cohort_archive_scoped(broker: str, account_id: str) -> CohortArchiveView:
    try:
        return await cohort_archive.get_cohort_archive_view(broker, account_id)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


@router.post(
    "/{broker}/accounts/{account_id}/bots/cohort-archive",
    response_model=CohortActionResult,
    summary="Archive a batch of finished bots with per-leg receipts (ADR 0052)",
)
async def run_cohort_archive_scoped(
    broker: str, account_id: str, request: CohortArchiveRequest
) -> CohortActionResult:
    try:
        result = await cohort_archive.run_cohort_archive(
            broker,
            account_id,
            request,
            operator_identity=settings.PANEL_OPERATOR_IDENTITY,
        )
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)
    for leg in result.legs:
        if leg.outcome in ("applied", "replayed"):
            schedule_live_projection_refresh(broker, account_id, leg.strategy_instance_id)
    return result


# ── §8 Chart endpoints (account-scoped + unscoped alias) ─────────────────────


async def _live_chart(
    broker: str,
    account_id: str,
    sid: str,
    resolution: Literal["5s", "1m"],
) -> ChartLiveResponse:
    try:
        return await ds.get_live_chart(
            broker,
            account_id,
            sid,
            resolution=resolution,
        )
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/chart/live",
    response_model=ChartLiveResponse,
    summary="LIVE chart pane: today's IBKR bars + fill markers (§8)",
)
async def get_live_chart_scoped(
    broker: str,
    account_id: str,
    sid: str,
    resolution: Literal["5s", "1m"] = Query("1m"),
) -> ChartLiveResponse:
    return await _live_chart(broker, account_id, sid, resolution)


@router.get(
    "/{broker}/bots/{sid}/chart/live",
    response_model=ChartLiveResponse,
    summary="LIVE chart pane (single-account alias) (§8)",
)
async def get_live_chart_unscoped(
    broker: str,
    sid: str,
    resolution: Literal["5s", "1m"] = Query("1m"),
) -> ChartLiveResponse:
    account_id = await _resolve_default_account(broker)
    return await _live_chart(broker, account_id, sid, resolution)


async def _history_chart(
    broker: str,
    account_id: str,
    sid: str,
    timeframe: str,
) -> ChartHistoryResponse:
    # The route signature already closes the enum at the schema boundary; this
    # coercion stays as the service-side guard for non-HTTP callers.
    try:
        coerced = coerce_history_timeframe(timeframe)
    except ChartTimeframeError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "why": None}) from None
    try:
        return await ds.get_history_chart(broker, account_id, sid, coerced)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/chart/history",
    response_model=ChartHistoryResponse,
    summary="Polygon chart: bounded timeframe window (§8)",
)
async def get_history_chart_scoped(
    broker: str,
    account_id: str,
    sid: str,
    timeframe: ChartHistoryTimeframe = Query(...),
) -> ChartHistoryResponse:
    return await _history_chart(broker, account_id, sid, timeframe)


@router.get(
    "/{broker}/bots/{sid}/chart/history",
    response_model=ChartHistoryResponse,
    summary="Polygon chart (single-account alias) (§8)",
)
async def get_history_chart_unscoped(
    broker: str,
    sid: str,
    timeframe: ChartHistoryTimeframe = Query(...),
) -> ChartHistoryResponse:
    account_id = await _resolve_default_account(broker)
    return await _history_chart(broker, account_id, sid, timeframe)


# ── §14 Operator-gated evidence (account-scoped + unscoped alias) ─────────────


async def _read_evidence(
    broker: str,
    account_id: str,
    sid: str,
    transaction_ref: str | None,
    cursor: str | None,
    page_size: int,
    client_hint: str | None,
) -> EvidencePage:
    try:
        await panel_scope.validate_account_scope(broker, account_id, sid)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)
    # read_evidence_page is outside the try/except: it raises only OSError
    # (audit log write failure, which is logged-and-swallowed inside) and does
    # not raise PanelDataError, so wrapping it here would mask real I/O errors.
    return read_evidence_page(
        account_id=account_id,
        sid=sid,
        transaction_ref=transaction_ref,
        cursor=cursor,
        page_size=page_size,
        operator_identity=settings.PANEL_OPERATOR_IDENTITY,
        client_hint=client_hint,
    )


@router.get(
    "/{broker}/accounts/{account_id}/bots/{sid}/evidence",
    response_model=EvidencePage,
    summary="Operator-gated raw evidence for one bot (bounded, paged, audit-logged) (§14)",
)
async def get_evidence_scoped(
    broker: str,
    account_id: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
    cursor: str | None = Query(default=None, max_length=1024),
    page_size: int = Query(default=PAGE_SIZE_DEFAULT, ge=1),
    client_hint: str | None = Query(default=None, max_length=256),
) -> EvidencePage:
    return await _read_evidence(broker, account_id, sid, transaction_ref, cursor, page_size, client_hint)


@router.get(
    "/{broker}/bots/{sid}/evidence",
    response_model=EvidencePage,
    summary="Operator-gated raw evidence (single-account alias) (§14)",
)
async def get_evidence_unscoped(
    broker: str,
    sid: str,
    transaction_ref: str | None = Query(default=None, max_length=256),
    cursor: str | None = Query(default=None, max_length=1024),
    page_size: int = Query(default=PAGE_SIZE_DEFAULT, ge=1),
    client_hint: str | None = Query(default=None, max_length=256),
) -> EvidencePage:
    account_id = await _resolve_default_account(broker)
    return await _read_evidence(broker, account_id, sid, transaction_ref, cursor, page_size, client_hint)


# ── Shared helpers ───────────────────────────────────────────────────────────


async def _resolve_default_account(broker: str) -> str:
    """Resolve the broker's single account for the unscoped alias routes.

    The unscoped forms serve the single-account case (§3); they resolve the
    real account and then delegate to the same validated path.
    """
    try:
        return await panel_scope.resolve_account_id(broker)
    except panel_errors.PanelDataError as error:
        _raise_panel_error(error)


# Re-export ``PanelAction`` for the OpenAPI schema (nested inside BotPanelView).
__all__ = ["PanelAction", "router"]
