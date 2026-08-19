"""Account-scoped reconciliation and triage endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.broker.ibkr import account as ibkr_account
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    repair_account_event_sequence,
)
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.journal_recovery_state import JournalRecoveryStateCorruptError
from app.routers.broker_dependencies import require_connected_client
from app.schemas.account_cockpit import AccountCockpitResponse
from app.schemas.account_directory import AccountServiceStatusResponse, AccountsRosterResponse
from app.schemas.account_events import (
    AccountEventKind,
    AccountEventsResponse,
    AccountEventView,
    TraderAccountEventsResponse,
)
from app.schemas.account_reconciliation import (
    AccountAcceptExposureOverrideRequest,
    AccountAcceptExposureOverrideResponse,
    AccountClearFreezeRequest,
    AccountClearFreezeResponse,
    AccountEventSequenceRepairReceipt,
    AccountReconciliationAutomationPolicy,
    AccountReconciliationAutomationPolicyUpdate,
    AccountReconciliationReceipt,
    AccountSessionPolicyUpdateRequest,
    AccountSessionPolicyUpdateResponse,
    AccountTriageResponse,
)
from app.schemas.account_safety_snapshot import (
    AccountSafetySnapshot,
    PresentedOperatorActionRejection,
    PresentedOperatorActionRejectionResponse,
    PresentedOperatorActionResult,
)
from app.schemas.journal_recovery import JournalRecoveryReceipt, JournalRecoveryRequest
from app.schemas.presented_operator_action import (
    PresentedOperatorActionInvocation,
)
from app.services.account_cockpit import AccountCockpitService
from app.services.account_directory import (
    AccountDirectoryError,
    AccountDirectoryService,
    CurrentBrokerAccount,
    UnknownAccountError,
)
from app.services.account_event_journal import AccountEventJournalError, AccountEventJournalService
from app.services.account_gate_policy import AccountGatePolicyService
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_safety_access import current_broker_account
from app.services.account_safety_snapshot import AccountSafetySnapshotService
from app.services.account_truth_refresh import account_truth_artifacts_root, refresh_account_truth_now
from app.services.journal_recovery import JournalRecoveryError, JournalRecoveryService
from app.services.presented_account_actions import (
    PresentedAccountActionService,
    PresentedActionOutcomeUnknownError,
    PresentedActionRejectedError,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
ConnectedIbkrClient = Annotated[IbkrClient, Depends(require_connected_client)]


def get_account_artifacts_root() -> Path:
    return account_truth_artifacts_root()


AccountArtifactsRoot = Annotated[Path, Depends(get_account_artifacts_root)]


def get_account_reconciliation_service(
    artifacts_root: AccountArtifactsRoot,
) -> AccountReconciliationService:
    """Build reconciliation service from the overridable artifact-root dependency."""

    return AccountReconciliationService(artifacts_root=artifacts_root)


def get_account_event_journal_service(
    artifacts_root: AccountArtifactsRoot,
) -> AccountEventJournalService:
    """Build the read-only Account desk journal projection."""

    return AccountEventJournalService(artifacts_root=artifacts_root)


def get_current_broker_account() -> CurrentBrokerAccount | None:
    """Expose the single currently connected broker account, if one exists."""

    return current_broker_account()


CurrentBrokerAccountDependency = Annotated[CurrentBrokerAccount | None, Depends(get_current_broker_account)]


def get_account_directory_service(
    artifacts_root: AccountArtifactsRoot,
    current_account: CurrentBrokerAccountDependency,
) -> AccountDirectoryService:
    """Build the read-only account directory from canonical broker/artifact facts."""

    return AccountDirectoryService(
        artifacts_root=artifacts_root,
        current_account=current_account,
        requested_account_gate_authority=get_settings().account_gate_authority,
    )


def get_account_safety_snapshot_service(
    artifacts_root: AccountArtifactsRoot,
    directory: Annotated[AccountDirectoryService, Depends(get_account_directory_service)],
) -> AccountSafetySnapshotService:
    """Compose existing account authorities without refreshing broker evidence."""

    # Preserve this injectable directory boundary for Account desk tests.
    # Other routers use the dedicated service factory instead of importing
    # this router just to obtain the same broker-free composition.
    return AccountSafetySnapshotService(artifacts_root=artifacts_root, directory=directory)


def get_presented_account_action_service(
    artifacts_root: AccountArtifactsRoot,
) -> PresentedAccountActionService:
    return PresentedAccountActionService(artifacts_root=artifacts_root)


def get_journal_recovery_service(artifacts_root: AccountArtifactsRoot) -> JournalRecoveryService:
    """Build the sole operator-required Clerk journal recovery ceremony."""

    return JournalRecoveryService(artifacts_root=artifacts_root)


def get_account_gate_policy_service(artifacts_root: AccountArtifactsRoot) -> AccountGatePolicyService:
    """Build the narrow account-gate mutation facade."""

    return AccountGatePolicyService(artifacts_root=artifacts_root)


def _account_directory_http_error(exc: AccountDirectoryError) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason_code": "ACCOUNT_SERVICE_ARTIFACT_CORRUPT",
            "message": "Account service evidence is unavailable because its durable artifacts are invalid.",
        },
    )


@router.get("", response_model=AccountsRosterResponse)
async def accounts_roster_endpoint(
    service: Annotated[AccountDirectoryService, Depends(get_account_directory_service)],
) -> AccountsRosterResponse:
    """List configured and durable-known accounts for the Account desk roster."""

    try:
        return service.roster()
    except AccountDirectoryError as exc:
        raise _account_directory_http_error(exc) from exc


@router.get("/{account_id}/clerk", response_model=AccountServiceStatusResponse)
async def account_service_status_endpoint(
    account_id: str,
    service: Annotated[AccountDirectoryService, Depends(get_account_directory_service)],
) -> AccountServiceStatusResponse:
    """Return the immutable Account service projection for one known account."""

    try:
        return service.service_status(account_id=_canonical_account_id(account_id))
    except UnknownAccountError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason_code": "ACCOUNT_UNKNOWN"}) from exc
    except AccountDirectoryError as exc:
        raise _account_directory_http_error(exc) from exc


@router.get("/{account_id}/safety-snapshot", response_model=AccountSafetySnapshot)
async def account_safety_snapshot_endpoint(
    account_id: str,
    service: Annotated[AccountSafetySnapshotService, Depends(get_account_safety_snapshot_service)],
) -> AccountSafetySnapshot:
    """Return the broker-free, versioned account safety composition."""

    try:
        return service.snapshot(account_id=_canonical_account_id(account_id))
    except UnknownAccountError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason_code": "ACCOUNT_UNKNOWN"}) from exc
    except AccountDirectoryError as exc:
        raise _account_directory_http_error(exc) from exc


@router.get("/{account_id}/cockpit", response_model=AccountCockpitResponse)
async def account_cockpit_endpoint(
    account_id: str,
    directory: Annotated[AccountDirectoryService, Depends(get_account_directory_service)],
) -> AccountCockpitResponse:
    """Return the account cockpit's authoritative posture and degraded mode."""

    canonical_account_id = _canonical_account_id(account_id)
    try:
        settings = get_settings()
        return await AccountCockpitService(
            directory=directory,
            # The operator cockpit is a startability/readiness surface, not
            # the sub-second connectivity monitor. The single-loop daemon may
            # spend more than two seconds composing concurrent bot facts, so
            # use the existing bounded 10-second readiness probe and avoid a
            # false "Daemon Down" while authenticated requests return 200.
            fetch_daemon_health=lambda: host_daemon_client.fetch_startability_health(settings.live_runner_daemon_url),
        ).surface(account_id=canonical_account_id)
    except UnknownAccountError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason_code": "ACCOUNT_UNKNOWN"}) from exc
    except AccountDirectoryError as exc:
        raise _account_directory_http_error(exc) from exc


@router.post("/{account_id}/journal-recovery/quarantine", response_model=JournalRecoveryReceipt)
async def quarantine_account_clerk_journal_endpoint(
    account_id: str,
    request: JournalRecoveryRequest,
    service: Annotated[JournalRecoveryService, Depends(get_journal_recovery_service)],
) -> JournalRecoveryReceipt:
    """Permanently rename aside corrupt journal evidence after typed confirmation."""

    if request.confirmation_token != "QUARANTINE":
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"reason_code": "JOURNAL_RECOVERY_QUARANTINE_CONFIRMATION_REQUIRED"}
        )
    try:
        return await run_in_threadpool(
            service.quarantine,
            account_id=_canonical_account_id(account_id),
            idempotency_key=request.idempotency_key,
        )
    except JournalRecoveryStateCorruptError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": "JOURNAL_RECOVERY_STATE_CORRUPT"}) from exc
    except JournalRecoveryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": str(exc)}) from exc


@router.post("/{account_id}/journal-recovery/rebaseline", response_model=JournalRecoveryReceipt)
async def rebaseline_account_clerk_journal_endpoint(
    account_id: str,
    request: JournalRecoveryRequest,
    service: Annotated[JournalRecoveryService, Depends(get_journal_recovery_service)],
    client: ConnectedIbkrClient,
) -> JournalRecoveryReceipt:
    """Seed a fresh journal from a fresh broker snapshot; never infer bot ownership."""

    if request.confirmation_token != "REBASELINE":
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"reason_code": "JOURNAL_RECOVERY_REBASELINE_CONFIRMATION_REQUIRED"}
        )
    canonical_account_id = _canonical_account_id(account_id)
    try:
        recovery_state = await run_in_threadpool(service.state, account_id=canonical_account_id)
        snapshot = None
        if recovery_state.phase == "REBASELINE_REQUIRED":
            snapshot = await ibkr_account.fetch_positions(client, allow_cache_fallback=False)
        return await run_in_threadpool(
            service.rebaseline,
            account_id=canonical_account_id,
            idempotency_key=request.idempotency_key,
            snapshot=snapshot,
        )
    except JournalRecoveryStateCorruptError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": "JOURNAL_RECOVERY_STATE_CORRUPT"}) from exc
    except JournalRecoveryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": str(exc)}) from exc
    except BrokerError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason_code": "JOURNAL_RECOVERY_BROKER_SNAPSHOT_UNAVAILABLE", "message": str(exc)},
        ) from exc


@router.post(
    "/{account_id}/events/repair-sequence",
    response_model=AccountEventSequenceRepairReceipt,
    status_code=status.HTTP_201_CREATED,
)
async def repair_account_event_sequence_endpoint(
    account_id: str,
    artifacts_root: AccountArtifactsRoot,
) -> AccountEventSequenceRepairReceipt:
    """Resequence a corrupt account-event journal without discarding evidence.

    Repairs an ACCOUNT_EVENTS_JOURNAL_CORRUPT feed whose JSON rows are valid but
    whose durable ``seq`` envelope was duplicated. Snapshots the original bytes
    beside the ledger, then atomically rewrites only the ``seq`` field under the
    ledger lock. Malformed or cross-account rows are refused, not silently dropped.
    """

    canonical_account_id = _canonical_account_id(account_id)
    try:
        result = await run_in_threadpool(
            repair_account_event_sequence,
            artifacts_root,
            canonical_account_id,
        )
    except AccountArtifactError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason_code": "ACCOUNT_EVENTS_REPAIR_UNSAFE", "message": str(exc)},
        ) from exc
    return AccountEventSequenceRepairReceipt(
        account_id=result.account_id,
        rewritten_rows=result.rewritten_rows,
        backup_path=str(result.backup_path) if result.backup_path is not None else None,
    )


@router.put("/{account_id}/session-policy", response_model=AccountSessionPolicyUpdateResponse)
async def update_account_session_policy_endpoint(
    account_id: str,
    request: AccountSessionPolicyUpdateRequest,
    service: Annotated[AccountGatePolicyService, Depends(get_account_gate_policy_service)],
) -> AccountSessionPolicyUpdateResponse:
    """Set the account-wide outside-live-session exception explicitly."""

    policy = await run_in_threadpool(
        service.update_session_policy,
        account_id=_canonical_account_id(account_id),
        allow_outside_live_session=request.allow_outside_live_session,
    )
    return AccountSessionPolicyUpdateResponse(
        account_id=policy.account_id,
        allow_outside_live_session=policy.allow_outside_live_session,
        updated_at_ms=policy.updated_at_ms,
    )


@router.post("/{account_id}/reconciliation", response_model=AccountReconciliationReceipt)
async def reconcile_account_endpoint(
    account_id: str,
    client: ConnectedIbkrClient,
    service: Annotated[
        AccountReconciliationService,
        Depends(get_account_reconciliation_service),
    ],
) -> AccountReconciliationReceipt:
    """Create a durable account reconciliation receipt from Account Truth."""
    canonical_account_id = _canonical_account_id(account_id)
    try:
        return await _reconcile_account(canonical_account_id, client, service)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


async def _reconcile_account(
    account_id: str,
    client: IbkrClient,
    service: AccountReconciliationService,
) -> AccountReconciliationReceipt:
    """Refresh read-only Account Truth and write its evidence receipt."""

    account_truth = await refresh_account_truth_now(
        client,
        account_id=account_id,
        context="account reconciliation",
        account_truth_observer=service.observe_account_truth,
        account_truth_failure_observer=service.observe_account_truth_failure,
    )
    return service.write_receipt(requested_account_id=account_id, account_truth=account_truth)


def _presented_action_rejection_http_error(
    *,
    reason_code: str,
    message: str,
    snapshot: AccountSafetySnapshot | None = None,
) -> HTTPException:
    """Return the one documented refusal envelope for snapshot-bound actions."""

    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail=PresentedOperatorActionRejection(
            reason_code=reason_code,
            message=message,
            snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            snapshot_version=None if snapshot is None else snapshot.snapshot_version,
        ).model_dump(mode="json"),
    )


@router.post(
    "/{account_id}/presented-actions/reconcile-now",
    response_model=PresentedOperatorActionResult,
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": "Action was durably claimed but its external outcome is not proven.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/PresentedOperatorActionResult"},
                },
            },
        },
        status.HTTP_409_CONFLICT: {
            "model": PresentedOperatorActionRejectionResponse,
            "description": "The presented action is stale, unavailable, or does not match its target.",
        },
    },
)
async def execute_presented_reconcile_action_endpoint(
    account_id: str,
    request: PresentedOperatorActionInvocation,
    client: ConnectedIbkrClient,
    reconciliation: Annotated[AccountReconciliationService, Depends(get_account_reconciliation_service)],
    snapshot_service: Annotated[AccountSafetySnapshotService, Depends(get_account_safety_snapshot_service)],
    action_service: Annotated[PresentedAccountActionService, Depends(get_presented_account_action_service)],
) -> PresentedOperatorActionResult | JSONResponse:
    """Execute only the currently presented Reconcile Now envelope."""

    canonical_account_id = _canonical_account_id(account_id)
    if request.target.account_id != canonical_account_id:
        raise _presented_action_rejection_http_error(
            reason_code="ACTION_TARGET_MISMATCH",
            message="This action belongs to a different account.",
        )
    replay_error: PresentedActionRejectedError | None = None
    try:
        prior_result = action_service.replay_if_claimed(request)
    except PresentedActionRejectedError as exc:
        replay_error = exc
        prior_result = None
    if prior_result is not None:
        return _presented_action_response(prior_result)
    current_snapshot = snapshot_service.snapshot(account_id=canonical_account_id)
    if replay_error is not None:
        raise _presented_action_rejection_http_error(
            reason_code=replay_error.reason_code,
            message=replay_error.message,
            snapshot=current_snapshot,
        ) from replay_error
    action = next((item for item in current_snapshot.actions if item.action_id == "reconcile_now"), None)
    if action is None:
        raise _presented_action_rejection_http_error(
            reason_code="ACTION_NOT_PRESENTED",
            message="This action is no longer presented for the current account safety state.",
            snapshot=current_snapshot,
        )
    try:
        result = await action_service.execute_reconcile(
            action=action,
            invocation=request,
            invoke=lambda: _reconcile_account(canonical_account_id, client, reconciliation),
            refreshed_snapshot_id=lambda: snapshot_service.snapshot(account_id=canonical_account_id).snapshot_id,
        )
        return _presented_action_response(result)
    except PresentedActionRejectedError as exc:
        raise _presented_action_rejection_http_error(
            reason_code=exc.reason_code,
            message=exc.message,
            snapshot=current_snapshot,
        ) from exc
    except PresentedActionOutcomeUnknownError as exc:
        return _presented_action_response(exc.result)


def _presented_action_response(
    result: PresentedOperatorActionResult,
) -> PresentedOperatorActionResult | JSONResponse:
    """Use 202 for claimed work whose terminal broker result is not yet known."""

    if result.state in {"IN_PROGRESS", "PENDING_PROOF", "OUTCOME_UNKNOWN"}:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=result.model_dump(mode="json"),
        )
    return result


@router.get(
    "/{account_id}/reconciliation/latest",
    response_model=AccountReconciliationReceipt,
)
async def latest_account_reconciliation_endpoint(
    account_id: str,
    service: Annotated[
        AccountReconciliationService,
        Depends(get_account_reconciliation_service),
    ],
) -> AccountReconciliationReceipt:
    """Return the latest account reconciliation receipt without sweeping IBKR."""
    try:
        receipt = service.read_latest_receipt(_canonical_account_id(account_id))
        if receipt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "account reconciliation receipt not found")
        return receipt
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.put(
    "/{account_id}/reconciliation/automation",
    response_model=AccountReconciliationAutomationPolicy,
)
async def update_account_reconciliation_automation_endpoint(
    account_id: str,
    request: AccountReconciliationAutomationPolicyUpdate,
    service: Annotated[
        AccountReconciliationService,
        Depends(get_account_reconciliation_service),
    ],
) -> AccountReconciliationAutomationPolicy:
    """Persist the account policy for bot-owned execution reconciliation."""
    try:
        return service.update_automation_policy(
            account_id=_canonical_account_id(account_id),
            enabled=request.enabled,
            updated_by=request.updated_by,
        )
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/{account_id}/triage", response_model=AccountTriageResponse)
async def account_triage_endpoint(
    account_id: str,
    service: Annotated[
        AccountReconciliationService,
        Depends(get_account_reconciliation_service),
    ],
) -> AccountTriageResponse:
    """Return the thin account recovery projection for an account."""
    try:
        return service.triage(account_id=_canonical_account_id(account_id))
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/{account_id}/events", response_model=AccountEventsResponse)
async def account_events_endpoint(
    account_id: str,
    service: Annotated[AccountEventJournalService, Depends(get_account_event_journal_service)],
    view: AccountEventView = "operations",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    kinds: Annotated[list[AccountEventKind] | None, Query()] = None,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    after_seq: Annotated[int | None, Query(ge=1)] = None,
) -> AccountEventsResponse:
    """Read a versioned, cursor-paginated projection of one account journal."""

    if before_seq is not None and after_seq is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason_code": "ACCOUNT_EVENTS_CURSOR_EXCLUSIVE",
                "message": "Use either before_seq or after_seq, not both.",
            },
        )
    try:
        return service.page(
            account_id=_canonical_account_id(account_id),
            view=view,
            limit=limit,
            kinds=frozenset(kinds or ()),
            before_seq=before_seq,
            after_seq=after_seq,
        )
    except AccountEventJournalError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_EVENTS_JOURNAL_CORRUPT",
                "message": "Account event history is unavailable because its journal is invalid.",
            },
        ) from exc


@router.get("/{account_id}/events/trader", response_model=TraderAccountEventsResponse)
async def trader_account_events_endpoint(
    account_id: str,
    service: Annotated[AccountEventJournalService, Depends(get_account_event_journal_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TraderAccountEventsResponse:
    """Return trader-authored outcomes through a schema with no receipt fields."""

    try:
        return service.trader_page(account_id=_canonical_account_id(account_id), limit=limit)
    except AccountEventJournalError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_EVENTS_JOURNAL_CORRUPT",
                "message": "Trader account activity is unavailable because its journal is invalid.",
            },
        ) from exc


@router.post(
    "/{account_id}/freeze/clear",
    response_model=AccountClearFreezeResponse,
)
async def clear_account_freeze_endpoint(
    account_id: str,
    request: AccountClearFreezeRequest,
    service: Annotated[
        AccountReconciliationService,
        Depends(get_account_reconciliation_service),
    ],
) -> AccountClearFreezeResponse:
    """Clear an active account freeze only from a fresh, newer clean receipt."""
    try:
        return service.clear_freeze_from_latest_receipt(
            account_id=_canonical_account_id(account_id),
            requested_by=request.requested_by,
            receipt_id=request.receipt_id,
            reason=request.reason,
        )
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post(
    "/{account_id}/freeze/accept-exposure-override",
    response_model=AccountAcceptExposureOverrideResponse,
)
async def accept_exposure_override_endpoint(
    account_id: str,
    request: AccountAcceptExposureOverrideRequest,
    service: Annotated[
        AccountReconciliationService,
        Depends(get_account_reconciliation_service),
    ],
) -> AccountAcceptExposureOverrideResponse:
    """Clear an exposure freeze by recording an audited operator override."""
    try:
        return service.accept_exposure_override(
            account_id=_canonical_account_id(account_id),
            requested_by=request.requested_by,
            reason=request.reason,
            strategy_instance_id=request.strategy_instance_id,
            run_id=request.run_id,
            bot_order_namespace=request.bot_order_namespace,
        )
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _canonical_account_id(account_id: str) -> str:
    try:
        return normalize_account_id(account_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
