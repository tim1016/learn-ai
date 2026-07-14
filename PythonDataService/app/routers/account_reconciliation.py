"""Account-scoped reconciliation and triage endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.account_registry import backfill_false_crash_registry_rows
from app.routers.broker_dependencies import require_connected_client
from app.schemas.account_reconciliation import (
    AccountAcceptExposureOverrideRequest,
    AccountAcceptExposureOverrideResponse,
    AccountClearFreezeRequest,
    AccountClearFreezeResponse,
    AccountFalseCrashBackfillResponse,
    AccountReconciliationAutomationPolicy,
    AccountReconciliationAutomationPolicyUpdate,
    AccountReconciliationReceipt,
    AccountTriageResponse,
    LegacyStaleClaimCandidatesResponse,
    LegacyStaleClaimRetirementReceipt,
    LegacyStaleClaimRetireRequest,
)
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_refresh import account_truth_artifacts_root, refresh_account_truth_now
from app.services.legacy_stale_claim_retirement import (
    LegacyStaleClaimRetirementError,
    LegacyStaleClaimRetirementService,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
ConnectedIbkrClient = Annotated[IbkrClient, Depends(require_connected_client)]


def get_account_artifacts_root() -> Path:
    return account_truth_artifacts_root()


AccountArtifactsRoot = Annotated[Path, Depends(get_account_artifacts_root)]


def get_account_reconciliation_service() -> AccountReconciliationService:
    return AccountReconciliationService(artifacts_root=get_account_artifacts_root())


def get_legacy_stale_claim_retirement_service() -> LegacyStaleClaimRetirementService:
    return LegacyStaleClaimRetirementService(artifacts_root=get_account_artifacts_root())


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
