from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.broker.ibkr.client import BrokerError
from app.routers.broker_dependencies import require_connected_client
from app.schemas.broker_capability import (
    BrokerCapabilityProbeResponse,
    BrokerCapabilityReadResponse,
)
from app.services.broker_capability_service import (
    BrokerCapabilityService,
    get_broker_capability_service,
)

router = APIRouter(prefix="/api/broker/capability", tags=["broker-capability"])


@router.post("/probe", response_model=BrokerCapabilityProbeResponse)
async def probe_broker_capability(
    symbols: Annotated[str, Query(min_length=1, max_length=128)] = "SPY,QQQ",
    service: BrokerCapabilityService = Depends(get_broker_capability_service),
) -> BrokerCapabilityProbeResponse:
    client = require_connected_client()
    parsed = _parse_symbols(symbols)
    try:
        snapshots = await service.probe(client, symbols=parsed)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return BrokerCapabilityProbeResponse(snapshots=snapshots)


@router.get("", response_model=BrokerCapabilityReadResponse)
async def read_broker_capability(
    service: BrokerCapabilityService = Depends(get_broker_capability_service),
) -> BrokerCapabilityReadResponse:
    return BrokerCapabilityReadResponse(snapshots=service.read_latest())


def _parse_symbols(raw: str) -> list[str]:
    symbols = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not symbols:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "symbols must be non-empty.")
    invalid = [symbol for symbol in symbols if not symbol.replace(".", "").replace("-", "").isalnum()]
    if invalid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"invalid symbols: {', '.join(invalid)}",
        )
    return list(dict.fromkeys(symbols))
