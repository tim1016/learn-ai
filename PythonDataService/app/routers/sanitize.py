"""Standalone sanitization endpoint for arbitrary market data"""
from fastapi import APIRouter
import logging

from app.models.requests import SanitizeRequest
from app.models.responses import SanitizeResponse
from app.services.sanitizer import DataSanitizer

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/sanitize", response_model=SanitizeResponse)
async def sanitize_data(request: SanitizeRequest):
    """Sanitize arbitrary market data using pandas-dq Fix_DQ.

    Accepts a JSON list of dicts with numeric market data fields.
    Returns the cleaned data with outliers removed, missing values filled,
    and types enforced.
    """
    logger.info(f"[Sanitize] Received {len(request.data)} records (quantile={request.quantile})")

    try:
        result = DataSanitizer.sanitize_generic(request.data, quantile=request.quantile)

        logger.info(
            f"[Sanitize] Complete: {result['summary']['cleaned_count']}/{result['summary']['original_count']} records retained"
        )

        return SanitizeResponse(
            success=True,
            data=result['data'],
            summary=result['summary']
        )
    except Exception as e:
        logger.error(f"[Sanitize] Error: {str(e)}")
        return SanitizeResponse(
            success=False,
            data=[],
            summary={},
            error=str(e)
        )
