"""API endpoint for the ticker reference catalog."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.ticker_catalog import SymbolCatalogEntry
from app.services.ticker_catalog_service import (
    TickerCatalogService,
    get_ticker_catalog_service,
)

router = APIRouter()
logger = logging.getLogger(__name__)

TickerCatalogServiceDep = Annotated[
    TickerCatalogService,
    Depends(get_ticker_catalog_service),
]


@router.get("/catalog", response_model=list[SymbolCatalogEntry])
async def ticker_catalog(
    catalog_service: TickerCatalogServiceDep,
) -> list[SymbolCatalogEntry]:
    """The complete US-stock reference catalog for the shared symbol picker.

    Serves every listed symbol — active and inactive — in the picker-row
    projection (ADR 0066): the client applies its surface's membership
    policy, with delisted symbols behind the backfill panel's explicit
    toggle so a survivorship-biased universe stays a visible choice. The
    walk is ``market="stocks"`` because the lake backfill pipeline can only
    cover stocks; nothing else is offered, so the ensure-coverage gate can
    never be handed a symbol it cannot fill.

    Served from the data-plane core (this router runs on the coordinator in
    the split fleet, the browser's ingress) rather than the broker surface:
    the coordinator must construct no provider broker client (FR-041), and
    a listing universe is market reference data — the Polygon account this
    process already owns — not broker-operator evidence.
    """
    try:
        return await catalog_service.get()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "[Tickers] Error fetching the symbol catalog",
            extra={"error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Symbol catalog is unavailable: {e!s}",
        ) from e
