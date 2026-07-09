"""Account-scoped reconciliation and triage endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.broker.ibkr.client import BrokerError, IbkrClient
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
    AccountReconciliationReceipt,
    AccountTriageResponse,
)
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_refresh import account_truth_artifacts_root, refresh_account_truth_now

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
ConnectedIbkrClient = Annotated[IbkrClient, Depends(require_connected_client)]


def get_account_artifacts_root() -> Path:
    return account_truth_artifacts_root()


AccountArtifactsRoot = Annotated[Path, Depends(get_account_artifacts_root)]


def get_account_reconciliation_service() -> AccountReconciliationService:
    return AccountReconciliationService(artifacts_root=get_account_artifacts_root())


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
