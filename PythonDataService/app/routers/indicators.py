"""API endpoints for technical indicator calculation"""
from fastapi import APIRouter, HTTPException, status
import logging

from app.services.ta_service import TechnicalAnalysisService
from app.models.requests import CalculateIndicatorsRequest
from app.models.responses import CalculateIndicatorsResponse

router = APIRouter()
logger = logging.getLogger(__name__)

ta_service = TechnicalAnalysisService()


@router.post("/calculate", response_model=CalculateIndicatorsResponse)
async def calculate_indicators(request: CalculateIndicatorsRequest):
    """Calculate technical indicators from OHLCV data."""
    try:
        logger.info(
            f"[TA] Calculating {len(request.indicators)} indicators "
            f"for {request.ticker} ({len(request.bars)} bars)"
        )

        bars_dicts = [bar.model_dump() for bar in request.bars]
        indicator_dicts = [ind.model_dump() for ind in request.indicators]

        results = ta_service.calculate_indicators(bars_dicts, indicator_dicts)

        return CalculateIndicatorsResponse(
            success=True,
            ticker=request.ticker,
            indicators=results
        )

    except Exception as e:
        logger.error(f"[TA] Error calculating indicators: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate indicators: {str(e)}"
        )
