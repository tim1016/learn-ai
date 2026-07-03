"""Account Truth and broker-ledger endpoints under /api/broker."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.models import (
    IbkrOpenOrder,
    IbkrOrderSpec,
    IbkrOrderWhatIfPreview,
)
from app.broker.ibkr.order_history import list_completed_orders
from app.broker.ibkr.order_previews import preview_paper_order
from app.broker.ibkr.orders import OrderRefusedError
from app.routers.broker_dependencies import require_connected_client
from app.schemas.account_truth import AccountTruthResponse
from app.services.account_truth_refresh import refresh_account_truth_now

router = APIRouter(prefix="/api/broker", tags=["broker"])
ConnectedIbkrClient = Annotated[IbkrClient, Depends(require_connected_client)]


@router.get("/account-truth", response_model=AccountTruthResponse)
async def account_truth_endpoint(client: ConnectedIbkrClient) -> AccountTruthResponse:
    """Account-wide ownership, risk, and invariant truth projection."""
    try:
        return await refresh_account_truth_now(client, context="account truth")
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/orders/what-if", response_model=IbkrOrderWhatIfPreview)
async def order_what_if_endpoint(
    spec: IbkrOrderSpec,
    client: ConnectedIbkrClient,
) -> IbkrOrderWhatIfPreview:
    """Preview paper-order margin/commission impact without submitting."""
    try:
        return await preview_paper_order(client, spec)
    except OrderRefusedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/orders/completed", response_model=list[IbkrOpenOrder])
async def list_completed_orders_endpoint(
    client: ConnectedIbkrClient,
) -> list[IbkrOpenOrder]:
    """Recent completed, cancelled, rejected, and inactive broker orders."""
    try:
        return await list_completed_orders(client)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
