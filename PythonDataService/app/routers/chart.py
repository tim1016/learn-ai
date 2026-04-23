"""API endpoints for chart data: resampled OHLCV + indicators with caching."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.chart_service import (
    TIMEFRAME_DEFS,
    get_allowed_timeframes,
    get_chart_data,
)
from app.services.dataset_service import INDICATOR_CONFIGS

router = APIRouter()
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Request / Response models
# ──────────────────────────────────────────────
class ChartIndicatorEntry(BaseModel):
    """Single indicator specification."""

    name: str = Field(..., min_length=1, description="Indicator name (e.g. 'ema', 'rsi', 'macd')")
    params: dict[str, Any] = Field(default_factory=dict, description="Indicator parameters")


class ChartDataRequest(BaseModel):
    """Request for chart data with resampled bars and indicators."""

    ticker: str = Field(..., min_length=1, max_length=20, description="Ticker symbol")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    timeframe: str = Field("1D", description="Timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 1D, 1W, 1M")
    session: str = Field("rth", description="'rth' for regular trading hours, 'extended' for all hours")
    forward_fill: bool = Field(False, description="Fill missing bars with previous close (volume=0)")
    indicators: list[ChartIndicatorEntry] = Field(
        default_factory=list,
        description="Indicators to compute on resampled bars",
    )
    compute_all_indicators: bool = Field(
        False,
        description="When True, compute all indicators with default params (ignores 'indicators' list)",
    )
    adjusted: bool = Field(True, description="Adjust for splits/dividends (Polygon default: true)")


class AllowedTimeframesRequest(BaseModel):
    """Request for allowed timeframes given a date range."""

    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    session: str = Field("rth", description="'rth' or 'extended'")


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────
@router.post("/data")
async def chart_data(request: ChartDataRequest):
    """
    Fetch resampled OHLCV bars with computed indicators.

    Two-layer caching:
    - Layer 1: resampled bars cached per (ticker, range, timeframe, session)
    - Layer 2: indicator results cached per (resample_key, canonical_indicators)

    Returns structured error codes on failure:
    - TIMEFRAME_NOT_ALLOWED: too many bars for requested timeframe
    - NO_DATA: no bars returned from Polygon
    - INVALID_RANGE: bad timeframe or date range
    - RATE_LIMITED: Polygon rate limit hit
    - INTERNAL_ERROR: unexpected failure
    """
    try:
        # Convert indicators to dict format
        indicator_dicts = [{"name": ind.name, "params": ind.params} for ind in request.indicators]

        # get_chart_data does heavy pandas work (Polygon fetch, resample, RTH
        # filter, indicator compute) — all synchronous. Running it directly in
        # the async handler blocked the event loop for 3-5 s and serialized
        # every other request on this worker (audit § 5.6 — availability
        # checks that normally take 5 ms measured 2.0 s head-of-line).
        # asyncio.to_thread offloads to the default thread pool so the loop
        # stays responsive.
        result = await asyncio.to_thread(
            get_chart_data,
            ticker=request.ticker,
            from_date=request.from_date,
            to_date=request.to_date,
            timeframe=request.timeframe,
            session=request.session,
            forward_fill=request.forward_fill,
            indicators=indicator_dicts,
            compute_all_indicators=request.compute_all_indicators,
            adjusted=request.adjusted,
        )

        # Check if result is an error
        if "error_code" in result:
            error_code = result["error_code"]
            if error_code == "TIMEFRAME_NOT_ALLOWED":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=result,
                )
            elif error_code == "NO_DATA":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=result,
                )
            elif error_code == "INVALID_RANGE":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=result,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=result,
                )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CHART] Error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "INTERNAL_ERROR",
                "detail": str(e),
            },
        )


@router.post("/allowed-timeframes")
async def allowed_timeframes(request: AllowedTimeframesRequest):
    """
    Return allowed timeframes for the given date range and session.
    Frontend should use this as the source of truth for timeframe availability.
    """
    try:
        allowed, estimates, recommended = get_allowed_timeframes(request.from_date, request.to_date, request.session)
        return {
            "allowed_timeframes": allowed,
            "estimated_bars_per_timeframe": estimates,
            "recommended_timeframe": recommended,
        }
    except Exception as e:
        logger.error(f"[CHART] Allowed timeframes error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_code": "INTERNAL_ERROR", "detail": str(e)},
        )


@router.get("/timeframes")
async def list_timeframes():
    """Return all supported timeframes with metadata."""
    return {"timeframes": [{"key": key, "minutes": val["minutes"]} for key, val in TIMEFRAME_DEFS.items()]}


@router.get("/available-indicators")
async def list_chart_indicators():
    """Return indicators available for chart overlays and panels."""
    return {
        "indicators": {
            name: {
                "params": configs,
                "panel": "main"
                if name
                in {
                    "ema",
                    "sma",
                    "dema",
                    "tema",
                    "wma",
                    "hma",
                    "kama",
                    "zlma",
                    "rma",
                    "alma",
                    "bbands",
                    "supertrend",
                    "vwap",
                    "psar",
                    "kc",
                    "donchian",
                }
                else name,
            }
            for name, configs in INDICATOR_CONFIGS.items()
        }
    }
