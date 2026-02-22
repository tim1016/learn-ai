"""API endpoints for options strategy analysis"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.strategy import StrategyAnalyzeRequest, StrategyAnalyzeResponse
from app.services.strategy_engine import analyze_strategy

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/analyze", response_model=StrategyAnalyzeResponse)
async def analyze_options_strategy(request: StrategyAnalyzeRequest) -> StrategyAnalyzeResponse:
    """Analyze an options strategy: payoff curve, POP, EV, breakevens."""
    try:
        logger.info(
            "[Strategy] Analyzing %d-leg strategy for %s",
            len(request.legs), request.symbol,
        )
        result = analyze_strategy(request)
        logger.info(
            "[Strategy] Analysis complete: POP=%.2f%%, EV=%.2f",
            result.pop * 100, result.expected_value,
        )
        return result
    except Exception as e:
        logger.error("[Strategy] Error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Strategy analysis failed: {str(e)}",
        )
