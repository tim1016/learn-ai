"""Account-scoped reconciliation and triage endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool

from app.broker.ibkr import account as ibkr_account
from app.broker.ibkr.client import BrokerError, IbkrClient, NotConnectedError, _is_paper_account, get_client
from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import AccountArtifactError, append_account_event
from app.engine.live.account_clerk_rpc import (
    AccountClerkRpcClient,
    AccountClerkRpcError,
    AccountClerkRpcRejectedError,
)
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.account_registry import backfill_false_crash_registry_rows
from app.engine.live.daemon_command_idempotency import (
    DaemonCommandIdempotencyService,
    DaemonCommandOutcome,
)
from app.engine.live.journal_recovery_state import JournalRecoveryStateCorruptError
from app.routers.broker_dependencies import require_connected_client
from app.schemas.account_cockpit import (
    AccountClerkRestoreReceipt,
    AccountClerkRestoreRequest,
    AccountCockpitResponse,
)
from app.schemas.account_directory import AccountServiceStatusResponse, AccountsRosterResponse
from app.schemas.account_events import AccountEventKind, AccountEventsResponse, AccountEventView
from app.schemas.account_reconciliation import (
    AccountAcceptExposureOverrideRequest,
    AccountAcceptExposureOverrideResponse,
    AccountClearFreezeRequest,
    AccountClearFreezeResponse,
    AccountClerkRestartSmokeRequest,
    AccountClerkRestartSmokeResponse,
    AccountFalseCrashBackfillResponse,
    AccountReconciliationAutomationPolicy,
    AccountReconciliationAutomationPolicyUpdate,
    AccountReconciliationReceipt,
    AccountSessionPolicyUpdateRequest,
    AccountSessionPolicyUpdateResponse,
    AccountTriageResponse,
    LegacyStaleClaimCandidatesResponse,
    LegacyStaleClaimRetirementReceipt,
    LegacyStaleClaimRetireRequest,
)
from app.schemas.journal_cures import (
    JournalCurePreview,
    JournalCureReceipt,
    JournalCureRequest,
    OperatorRecoveryFlattenRequest,
    OperatorRecoveryFlattenResponse,
)
from app.schemas.journal_recovery import JournalRecoveryReceipt, JournalRecoveryRequest
from app.schemas.live_runs import (
    AccountEmergencyFlattenDispatchRequest,
    AccountEmergencyFlattenResponse,
    EmergencyFlattenRequest,
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
from app.services.account_gate_promotion import AccountGatePromotionError
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_refresh import account_truth_artifacts_root, refresh_account_truth_now
from app.services.journal_cures import JournalCureError, JournalCureService
from app.services.journal_recovery import JournalRecoveryError, JournalRecoveryService
from app.services.legacy_stale_claim_retirement import (
    LegacyStaleClaimRetirementError,
    LegacyStaleClaimRetirementService,
)
from app.utils.timestamps import now_ms_utc

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
ConnectedIbkrClient = Annotated[IbkrClient, Depends(require_connected_client)]


def get_account_artifacts_root() -> Path:
    return account_truth_artifacts_root()


AccountArtifactsRoot = Annotated[Path, Depends(get_account_artifacts_root)]


def get_account_clerk_restore_idempotency(
    artifacts_root: AccountArtifactsRoot,
) -> DaemonCommandIdempotencyService:
    """Build the durable one-shot envelope for the Clerk restore control."""

    return DaemonCommandIdempotencyService(artifacts_root)


AccountClerkRestoreIdempotency = Annotated[
    DaemonCommandIdempotencyService,
    Depends(get_account_clerk_restore_idempotency),
]


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

    try:
        client = get_client()
    except NotConnectedError:
        return None
    if not client.is_connected():
        return None
    account_id = client.connected_account
    if account_id is None:
        return None
    return CurrentBrokerAccount(account_id=account_id, is_paper=_is_paper_account(account_id))


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


def get_legacy_stale_claim_retirement_service() -> LegacyStaleClaimRetirementService:
    return LegacyStaleClaimRetirementService(artifacts_root=get_account_artifacts_root())


def get_journal_cure_service(artifacts_root: AccountArtifactsRoot) -> JournalCureService:
    """Build cure projection from the overridable account-artifact root."""

    return JournalCureService(artifacts_root=artifacts_root)


def get_journal_recovery_service(artifacts_root: AccountArtifactsRoot) -> JournalRecoveryService:
    """Build the sole operator-required Clerk journal recovery ceremony."""

    return JournalRecoveryService(artifacts_root=artifacts_root)


def get_account_gate_policy_service(artifacts_root: AccountArtifactsRoot) -> AccountGatePolicyService:
    """Build the narrow account-gate mutation facade."""

    return AccountGatePolicyService(artifacts_root=artifacts_root)


def _outcome_unknown_http_error(
    exc: host_daemon_client.HostDaemonOutcomeUnknownError,
    *,
    idempotency_key: str | None = None,
) -> HTTPException:
    """Preserve ambiguous daemon mutations as a refresh-before-retry response."""

    detail: dict[str, str] = {
        "reason_code": "OUTCOME_UNKNOWN",
        "message": exc.detail or "The Clerk readiness request may have completed; refresh before retrying.",
    }
    if idempotency_key is not None:
        detail["idempotency_key"] = idempotency_key
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail=detail,
    )


def _clerk_rpc_http_error(exc: AccountClerkRpcError) -> HTTPException:
    """Expose normal Clerk rejections without collapsing them into a server error."""

    unavailable = exc.reason_code.startswith("ACCOUNT_CLERK_UNAVAILABLE:")
    reason_code = exc.reason if isinstance(exc, AccountClerkRpcRejectedError) else exc.reason_code
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE if unavailable else status.HTTP_409_CONFLICT,
        detail={"reason_code": reason_code, "message": "Clerk rejected or could not complete the request."},
    )


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
            fetch_daemon_health=lambda: host_daemon_client.fetch_health(
                settings.live_runner_daemon_url
            ),
        ).surface(account_id=canonical_account_id)
    except UnknownAccountError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"reason_code": "ACCOUNT_UNKNOWN"}) from exc
    except AccountDirectoryError as exc:
        raise _account_directory_http_error(exc) from exc


@router.post("/{account_id}/clerk/restore", response_model=AccountClerkRestoreReceipt)
async def restore_account_clerk_endpoint(
    account_id: str,
    request: AccountClerkRestoreRequest,
    artifacts_root: AccountArtifactsRoot,
    directory: Annotated[AccountDirectoryService, Depends(get_account_directory_service)],
    idempotency: AccountClerkRestoreIdempotency,
) -> AccountClerkRestoreReceipt:
    """Restore the sole Clerk through the daemon and leave a durable receipt."""

    canonical_account_id = _canonical_account_id(account_id)

    def restore_once() -> DaemonCommandOutcome:
        """Invoke the host-only actuator after claiming this exact operation."""

        settings = get_settings()
        asyncio.run(
            host_daemon_client.ensure_account_clerk(
                settings.live_runner_daemon_url,
                canonical_account_id,
            )
        )
        clerk = directory.service_status(account_id=canonical_account_id)
        if clerk.attachment != "ATTACHED" or clerk.generation is None:
            raise _AccountClerkRestoreUnconfirmedError
        recorded_at_ms = now_ms_utc()
        receipt = AccountClerkRestoreReceipt(
            receipt_id=f"account-clerk-restore:{request.idempotency_key}",
            account_id=canonical_account_id,
            clerk_generation=clerk.generation,
            recorded_at_ms=recorded_at_ms,
        )
        append_account_event(
            artifacts_root,
            canonical_account_id,
            {
                "event_type": "account_clerk_restore_completed",
                **receipt.model_dump(),
            },
            only_if_receipt_absent=True,
        )
        return DaemonCommandOutcome(
            status_code=status.HTTP_200_OK,
            body=receipt.model_dump(mode="json"),
            replayed=False,
        )

    try:
        outcome = await run_in_threadpool(
            idempotency.execute,
            idempotency_key=request.idempotency_key,
            command="account_clerk_restore",
            account_id=canonical_account_id,
            semantic_payload={"confirmation_token": request.confirmation_token},
            invoke=restore_once,
            enforcement_enabled=True,
        )
        if outcome.status_code != status.HTTP_200_OK:
            raise HTTPException(outcome.status_code, detail=outcome.body)
        return AccountClerkRestoreReceipt.model_validate(outcome.body)
    except _AccountClerkRestoreUnconfirmedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_CLERK_RESTORE_UNCONFIRMED",
                "message": "The daemon returned, but a healthy Clerk generation was not re-observed.",
            },
        ) from exc
    except HTTPException:
        raise
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        raise _outcome_unknown_http_error(exc, idempotency_key=request.idempotency_key) from exc
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
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
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": "JOURNAL_RECOVERY_QUARANTINE_CONFIRMATION_REQUIRED"})
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
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": "JOURNAL_RECOVERY_REBASELINE_CONFIRMATION_REQUIRED"})
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
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail={"reason_code": "JOURNAL_RECOVERY_BROKER_SNAPSHOT_UNAVAILABLE", "message": str(exc)}) from exc


class _AccountClerkRestoreUnconfirmedError(RuntimeError):
    """A claimed restore cannot safely be called complete without a re-observation."""


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


@router.post("/{account_id}/gate-promotion/restart-smoke", response_model=AccountClerkRestartSmokeResponse)
async def record_account_clerk_restart_smoke_endpoint(
    account_id: str,
    request: AccountClerkRestartSmokeRequest,
    service: Annotated[AccountGatePolicyService, Depends(get_account_gate_policy_service)],
) -> AccountClerkRestartSmokeResponse:
    """Record the typed restart smoke for the current accepting Clerk."""

    canonical_account_id = _canonical_account_id(account_id)
    try:
        smoke = await run_in_threadpool(
            service.record_restart_smoke,
            account_id=canonical_account_id,
            confirmation=request.confirmation,
        )
    except AccountGatePromotionError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason_code": str(exc), "message": "Clerk restart smoke cannot be recorded yet."},
        ) from exc
    return AccountClerkRestartSmokeResponse(
        account_id=canonical_account_id,
        clerk_generation=smoke.clerk_generation,
        recorded_at_ms=smoke.recorded_at_ms,
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
        settings = get_settings()
        await host_daemon_client.ensure_account_clerk(
            settings.live_runner_daemon_url,
            canonical_account_id,
        )
        account_truth = await refresh_account_truth_now(
            client,
            account_id=canonical_account_id,
            context="account reconciliation",
            account_truth_observer=service.observe_account_truth,
            account_truth_failure_observer=service.observe_account_truth_failure,
        )
        return service.write_receipt(
            requested_account_id=canonical_account_id,
            account_truth=account_truth,
        )
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        raise _outcome_unknown_http_error(exc) from exc
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get(
    "/{account_id}/legacy-stale-claims/candidates",
    response_model=LegacyStaleClaimCandidatesResponse,
)
async def legacy_stale_claim_candidates_endpoint(
    account_id: str,
    client: ConnectedIbkrClient,
    service: Annotated[
        LegacyStaleClaimRetirementService,
        Depends(get_legacy_stale_claim_retirement_service),
    ],
) -> LegacyStaleClaimCandidatesResponse:
    """Return only legacy sidecar claims whose retirement is proven safe now."""

    canonical_account_id = _canonical_account_id(account_id)
    try:
        account_truth = await refresh_account_truth_now(
            client,
            account_id=canonical_account_id,
            context="legacy stale-claim candidate proof",
        )
        settings = get_settings()
        candidates = await service.candidates(
            account_id=canonical_account_id,
            account_truth=account_truth,
            fetch_run_process=lambda run_id: host_daemon_client.fetch_run_process(
                settings.live_runner_daemon_url,
                run_id,
            ),
        )
        return LegacyStaleClaimCandidatesResponse(
            account_id=canonical_account_id,
            generated_at_ms=account_truth.generated_at_ms,
            candidates=candidates,
        )
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post(
    "/{account_id}/legacy-stale-claims/retire",
    response_model=LegacyStaleClaimRetirementReceipt,
)
async def retire_legacy_stale_claim_endpoint(
    account_id: str,
    request: LegacyStaleClaimRetireRequest,
    client: ConnectedIbkrClient,
    service: Annotated[
        LegacyStaleClaimRetirementService,
        Depends(get_legacy_stale_claim_retirement_service),
    ],
) -> LegacyStaleClaimRetirementReceipt:
    """Retire one pre-Clerk claim only after re-proving every safety fact."""

    canonical_account_id = _canonical_account_id(account_id)
    try:
        account_truth = await refresh_account_truth_now(
            client,
            account_id=canonical_account_id,
            context="legacy stale-claim retirement",
        )
        settings = get_settings()
        return await service.retire(
            account_id=canonical_account_id,
            strategy_instance_id=request.strategy_instance_id,
            run_id=request.run_id,
            symbol=request.symbol,
            requested_by=request.requested_by,
            account_truth=account_truth,
            fetch_run_process=lambda run_id: host_daemon_client.fetch_run_process(
                settings.live_runner_daemon_url,
                run_id,
            ),
        )
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except LegacyStaleClaimRetirementError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason_code": exc.reason_code, "message": exc.detail},
        ) from exc
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/{account_id}/journal-cures", response_model=JournalCureReceipt, status_code=status.HTTP_201_CREATED)
async def apply_journal_cure_endpoint(
    account_id: str,
    request: JournalCureRequest,
    artifacts_root: AccountArtifactsRoot,
) -> JournalCureReceipt:
    """Append a claim-reducing cure only after the account Clerk is healthy."""

    canonical_account_id = _canonical_account_id(account_id)
    try:
        settings = get_settings()
        await host_daemon_client.ensure_account_clerk(settings.live_runner_daemon_url, canonical_account_id)
        return await AccountClerkRpcClient(
            artifacts_root=artifacts_root,
            account_id=canonical_account_id,
        ).apply_operator_adjustment(request)
    except JournalCureError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason_code": exc.reason_code, "message": exc.detail},
        ) from exc
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        raise _outcome_unknown_http_error(exc) from exc
    except AccountClerkRpcError as exc:
        raise _clerk_rpc_http_error(exc) from exc
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc


@router.get("/{account_id}/journal-cures/preview", response_model=JournalCurePreview)
async def journal_cure_preview_endpoint(
    account_id: str,
    bot_order_namespace: str,
    symbol: str,
    service: Annotated[JournalCureService, Depends(get_journal_cure_service)],
) -> JournalCurePreview:
    """Read the server-owned claim state before an operator creates a cure."""

    return await run_in_threadpool(
        service.preview,
        account_id=_canonical_account_id(account_id),
        bot_order_namespace=bot_order_namespace,
        symbol=symbol,
    )


@router.post("/{account_id}/operator-recovery-flatten", response_model=OperatorRecoveryFlattenResponse)
async def operator_recovery_flatten_endpoint(
    account_id: str,
    request: OperatorRecoveryFlattenRequest,
    artifacts_root: AccountArtifactsRoot,
) -> OperatorRecoveryFlattenResponse:
    """Run the existing retired-namespace flatten lane after Clerk readiness proof."""

    canonical_account_id = _canonical_account_id(account_id)
    if request.intent.account_id != canonical_account_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "intent account_id does not match path")
    try:
        settings = get_settings()
        await host_daemon_client.ensure_account_clerk(settings.live_runner_daemon_url, canonical_account_id)
        receipt = await AccountClerkRpcClient(
            artifacts_root=artifacts_root,
            account_id=canonical_account_id,
        ).submit_operator_recovery_flatten(request.intent)
        append_account_event(
            artifacts_root,
            canonical_account_id,
            {
                "event_type": "account_clerk_operator_recovery_flatten",
                "intent_id": request.intent.intent_id,
                "order_ref": request.intent.order_ref,
                "request_provenance": request.request_provenance,
                "recorded_at_ms": receipt.broker_acked.recorded_at_ms,
                "receipt_id": (
                    f"account-clerk-operator-recovery:"
                    f"{request.intent.intent_id}:{receipt.broker_acked.journal_seq}"
                ),
            },
            only_if_receipt_absent=True,
        )
        return OperatorRecoveryFlattenResponse(recovery_flatten=receipt)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        raise _outcome_unknown_http_error(exc) from exc
    except AccountClerkRpcError as exc:
        raise _clerk_rpc_http_error(exc) from exc


@router.post(
    "/{account_id}/emergency-flatten",
    response_model=AccountEmergencyFlattenResponse,
)
async def emergency_flatten_account_endpoint(
    account_id: str,
    request: EmergencyFlattenRequest,
    artifacts_root: AccountArtifactsRoot,
    reconciliation: Annotated[
        AccountReconciliationService,
        Depends(get_account_reconciliation_service),
    ],
) -> AccountEmergencyFlattenResponse:
    """Authorize and dispatch one Clerk-owned account-wide paper flatten."""

    canonical_account_id = _canonical_account_id(account_id)
    if request.account != canonical_account_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "request account does not match path")
    triage = reconciliation.triage(account_id=canonical_account_id)
    receipt = triage.account_reconciliation_receipt
    if (
        triage.emergency_flatten_confirmation is None
        or receipt is None
        or triage.recovery_flatten_candidates
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_EMERGENCY_FLATTEN_NOT_DECLARED",
                "message": "Fresh paper-account evidence does not declare an emergency flatten safe.",
            },
        )
    idempotency_key = request.idempotency_key
    try:
        settings = get_settings()
        await host_daemon_client.ensure_account_clerk(
            settings.live_runner_daemon_url,
            canonical_account_id,
        )
        authorization = await AccountClerkRpcClient(
            artifacts_root=artifacts_root,
            account_id=canonical_account_id,
        ).authorize_emergency_flatten(
            operation_id=idempotency_key,
            reconciliation_evidence_version=receipt.receipt_id,
        )
        payload = await host_daemon_client.emergency_flatten_account(
            settings.live_runner_daemon_url,
            canonical_account_id,
            AccountEmergencyFlattenDispatchRequest(
                account=canonical_account_id,
                confirmation_token="FLATTEN",
                idempotency_key=idempotency_key,
                authorization_id=authorization.authorization_id,
            ).model_dump(mode="json"),
        )
        response = AccountEmergencyFlattenResponse.model_validate(payload)
        response = response.model_copy(
            update={"idempotency_key": response.idempotency_key or idempotency_key}
        )
        append_account_event(
            artifacts_root,
            canonical_account_id,
            {
                "event_type": "account_emergency_flatten_completed",
                "audit_run_id": response.audit_run_id,
                "recorded_at_ms": response.completed_at_ms,
                "receipt_id": f"account-emergency-flatten:{response.audit_run_id}",
            },
            only_if_receipt_absent=True,
        )
        return response
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        raise _outcome_unknown_http_error(exc, idempotency_key=idempotency_key) from exc
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    except AccountClerkRpcError as exc:
        raise _clerk_rpc_http_error(exc) from exc


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


@router.post(
    "/{account_id}/registry/backfill-false-crashes",
    response_model=AccountFalseCrashBackfillResponse,
)
async def backfill_false_crash_registry_rows_endpoint(
    account_id: str,
    artifacts_root: AccountArtifactsRoot,
) -> AccountFalseCrashBackfillResponse:
    """Repair latest crash-retired registry rows disproven by durable run status."""
    try:
        result = backfill_false_crash_registry_rows(
            artifacts_root,
            account_id=_canonical_account_id(account_id),
        )
        return AccountFalseCrashBackfillResponse(
            accounts_scanned=result.accounts_scanned,
            candidate_rows=result.candidate_rows,
            rows_repaired=result.rows_repaired,
            rows_skipped_no_disproof=result.rows_skipped_no_disproof,
            invalid_account_dirs=result.invalid_account_dirs,
            repaired_run_ids=list(result.repaired_run_ids),
        )
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _canonical_account_id(account_id: str) -> str:
    try:
        return normalize_account_id(account_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
