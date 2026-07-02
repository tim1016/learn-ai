"""Account-scoped reconciliation and triage endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.broker.ibkr.account_truth import (
    fetch_account_truth,
    load_account_instance_registry_evidence,
)
from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.config import get_settings
from app.broker.ibkr.health import build_broker_health
from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_identity import normalize_account_id
from app.routers.broker_dependencies import require_connected_client
from app.schemas.account_reconciliation import (
    AccountReconciliationReceipt,
    AccountTriageResponse,
)
from app.services.account_reconciliation import AccountReconciliationService

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
ConnectedIbkrClient = Annotated[IbkrClient, Depends(require_connected_client)]


def get_account_reconciliation_service() -> AccountReconciliationService:
    artifacts_root = Path(get_settings().live_runs_root).parent
    return AccountReconciliationService(artifacts_root=artifacts_root)


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
    health = build_broker_health(client, get_monitor())
    registry_evidence = load_account_instance_registry_evidence(
        artifacts_root=Path(get_settings().live_runs_root).parent,
        account_id=canonical_account_id,
        context="account reconciliation",
    )
    try:
        account_truth = await fetch_account_truth(
            client,
            health=health,
            account_instance_bindings=registry_evidence.bindings,
            initial_evidence_gaps=registry_evidence.evidence_gaps,
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


def _canonical_account_id(account_id: str) -> str:
    try:
        return normalize_account_id(account_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
