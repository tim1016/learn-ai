"""Account Truth and broker-ledger endpoints under /api/broker."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.broker.ibkr.account_truth import fetch_account_truth
from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.health import build_broker_health
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
from app.services.broker_activity_publisher_registry import get_publisher_registry

router = APIRouter(prefix="/api/broker", tags=["broker"])
ConnectedIbkrClient = Annotated[IbkrClient, Depends(require_connected_client)]


@router.get("/account-truth", response_model=AccountTruthResponse)
async def account_truth_endpoint(client: ConnectedIbkrClient) -> AccountTruthResponse:
    """Account-wide ownership, risk, and invariant truth projection."""
    health = build_broker_health(client, get_monitor())
    try:
        return await fetch_account_truth(
            client,
            health=health,
            known_strategy_instance_ids=get_publisher_registry().instances(),
        )
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
