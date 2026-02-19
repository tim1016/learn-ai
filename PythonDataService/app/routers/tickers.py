"""API endpoints for ticker reference data (list, details, related companies)"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
import logging

from app.services.polygon_client import PolygonClientService
from app.models.requests import TickerListRequest, TickerDetailRequest, RelatedTickersRequest
from app.models.responses import (
    TickerListResponse, TickerInfo,
    TickerDetailResponse, TickerAddress,
    RelatedTickersResponse,
)

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
        logger.error(f"[Tickers] Error listing tickers: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list tickers: {str(e)}",
        )


@router.post("/details", response_model=TickerDetailResponse)
async def get_ticker_details(request: TickerDetailRequest) -> TickerDetailResponse:
    """Fetch detailed overview for a single stock ticker."""
    try:
        logger.info(f"[Tickers] Details request: {request.ticker}")

        raw = polygon_client.get_ticker_details(request.ticker)

        addr_raw = raw.get('address')
        address = TickerAddress(**addr_raw) if addr_raw else None

        return TickerDetailResponse(
            success=True,
            ticker=raw.get('ticker', request.ticker),
            name=raw.get('name', ''),
            description=raw.get('description'),
            market_cap=raw.get('market_cap'),
            homepage_url=raw.get('homepage_url'),
            total_employees=raw.get('total_employees'),
            list_date=raw.get('list_date'),
            sic_description=raw.get('sic_description'),
            primary_exchange=raw.get('primary_exchange'),
            type=raw.get('type'),
            weighted_shares_outstanding=raw.get('weighted_shares_outstanding'),
            address=address,
        )

    except Exception as e:
        logger.error(f"[Tickers] Error fetching details for {request.ticker}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch ticker details: {str(e)}",
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
        logger.error(f"[Tickers] Error fetching related for {request.ticker}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch related tickers: {str(e)}",
        )
