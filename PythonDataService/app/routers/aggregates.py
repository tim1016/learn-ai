"""API endpoints for aggregate bars (OHLCV data)"""
from fastapi import APIRouter, HTTPException, status
from typing import Optional
import logging

from app.services.polygon_client import PolygonClientService
from app.services.sanitizer import DataSanitizer
from app.models.requests import AggregateRequest
from app.models.responses import SanitizedDataResponse

router = APIRouter()
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()
sanitizer = DataSanitizer()


@router.post("/fetch", response_model=SanitizedDataResponse)
async def fetch_aggregates(request: AggregateRequest):
    """
    Fetch and sanitize aggregate bars (OHLCV) for a ticker

    - **ticker**: Stock symbol (e.g., AAPL, MSFT)
    - **multiplier**: Size of timespan multiplier (e.g., 1, 5, 15)
    - **timespan**: Size of time window (minute, hour, day, week, month)
    - **from_date**: Start date (YYYY-MM-DD)
    - **to_date**: End date (YYYY-MM-DD)
    - **limit**: Maximum number of results (default 50000)

    Returns sanitized OHLCV data with summary statistics
    """
    try:
        # Fetch raw data from Polygon
        raw_data = polygon_client.fetch_aggregates(
            ticker=request.ticker,
            multiplier=request.multiplier,
            timespan=request.timespan,
            from_date=request.from_date,
            to_date=request.to_date,
            limit=request.limit
        )

        # Sanitize with pandas
        sanitized_result = sanitizer.sanitize_aggregates(raw_data)

        return SanitizedDataResponse(
            success=True,
            data=sanitized_result['data'],
            summary=sanitized_result['summary'],
            ticker=request.ticker,
            data_type='aggregates'
        )

    except Exception as e:
        logger.error(f"Error in fetch_aggregates: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch aggregates: {str(e)}"
        )
