"""API endpoints for ticker reference data (list, details, related companies)"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

from fastapi import APIRouter, HTTPException, status

from app.models.requests import RelatedTickersRequest, TickerDetailRequest, TickerListRequest
from app.models.responses import (
    RelatedTickersResponse,
    SymbolCatalogEntry,
    TickerAddress,
    TickerDetailResponse,
    TickerInfo,
    TickerListResponse,
)
from app.services.polygon_client import PolygonClientService

router = APIRouter()
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()


@router.post("/list", response_model=TickerListResponse)
async def list_tickers(request: TickerListRequest) -> TickerListResponse:
    """Fetch basic reference info for a batch of stock tickers."""
    try:
        logger.info(f"[Tickers] List request: {len(request.tickers)} tickers")

        raw = polygon_client.list_tickers(request.tickers)

        tickers = [TickerInfo(**item) for item in raw]

        logger.info(f"[Tickers] Returning {len(tickers)} tickers")
        return TickerListResponse(success=True, tickers=tickers, count=len(tickers))

    except Exception as e:
        logger.error(f"[Tickers] Error listing tickers: {e!s}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list tickers: {e!s}",
        )


@router.post("/details", response_model=TickerDetailResponse)
async def get_ticker_details(request: TickerDetailRequest) -> TickerDetailResponse:
    """Fetch detailed overview for a single stock ticker."""
    try:
        logger.info(f"[Tickers] Details request: {request.ticker}")

        raw = polygon_client.get_ticker_details(request.ticker)

        addr_raw = raw.get("address")
        address = TickerAddress(**addr_raw) if addr_raw else None

        return TickerDetailResponse(
            success=True,
            ticker=raw.get("ticker", request.ticker),
            name=raw.get("name", ""),
            description=raw.get("description"),
            market_cap=raw.get("market_cap"),
            homepage_url=raw.get("homepage_url"),
            total_employees=raw.get("total_employees"),
            list_date=raw.get("list_date"),
            sic_description=raw.get("sic_description"),
            primary_exchange=raw.get("primary_exchange"),
            type=raw.get("type"),
            weighted_shares_outstanding=raw.get("weighted_shares_outstanding"),
            address=address,
        )

    except Exception as e:
        logger.error(f"[Tickers] Error fetching details for {request.ticker}: {e!s}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch ticker details: {e!s}",
        )


@router.post("/related", response_model=RelatedTickersResponse)
async def get_related_tickers(request: RelatedTickersRequest) -> RelatedTickersResponse:
    """Fetch related company tickers for a given stock."""
    try:
        logger.info(f"[Tickers] Related request: {request.ticker}")

        related = polygon_client.get_related_companies(request.ticker)

        return RelatedTickersResponse(
            success=True,
            ticker=request.ticker,
            related=related,
        )

    except Exception as e:
        logger.error(f"[Tickers] Error fetching related for {request.ticker}: {e!s}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch related tickers: {e!s}",
        )


# The catalog is one paginated vendor walk (~30k reference rows) serving
# every symbol picker in the browser, cached here so a dropdown-open burst
# never re-walks it. The cache holds the in-flight task, not a materialized
# list: concurrent misses single-flight onto one walk, and a failed walk
# drops its entry so the picker's Retry reaches the vendor instead of a
# sticky error. The TTL is long because reference data moves on listing/
# delisting cadence, not per request.
_CATALOG_CACHE_TTL_S = 3600.0
_catalog_entry: tuple[float, asyncio.Task[list[SymbolCatalogEntry]]] | None = None


def clear_ticker_catalog_cache_for_testing() -> None:
    """Drop the cached catalog so a test's stubbed client is consulted."""
    global _catalog_entry
    _catalog_entry = None


async def _load_catalog() -> list[SymbolCatalogEntry]:
    entries = await asyncio.to_thread(polygon_client.list_catalog_tickers)
    return [SymbolCatalogEntry(**entry) for entry in entries]


async def _await_catalog(
    entry: tuple[float, asyncio.Task[list[SymbolCatalogEntry]]],
) -> list[SymbolCatalogEntry]:
    global _catalog_entry
    try:
        # Shielded: a browser disconnect cancels this request handler, never
        # the shared walk — concurrent callers keep coalescing onto it.
        return await asyncio.shield(entry[1])
    except asyncio.CancelledError:
        raise
    except BaseException:
        # A failed walk is not cached: the next caller re-loads instead of
        # every picker on the page staring at a sticky error.
        if _catalog_entry is entry:
            _catalog_entry = None
        raise


@router.get("/catalog", response_model=list[SymbolCatalogEntry])
async def ticker_catalog() -> list[SymbolCatalogEntry]:
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
    global _catalog_entry
    entry = _catalog_entry
    if entry is not None:
        loaded_at, task = entry
        if not task.done() or monotonic() - loaded_at < _CATALOG_CACHE_TTL_S:
            return await _await_catalog(entry)
    entry = (monotonic(), asyncio.ensure_future(_load_catalog()))
    _catalog_entry = entry
    try:
        return await _await_catalog(entry)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Tickers] Error fetching the symbol catalog: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Symbol catalog is unavailable: {e!s}",
        ) from e
