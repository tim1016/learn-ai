"""API endpoints for chart data: resampled OHLCV + indicators with caching."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.chart import (
    AllowedTimeframesRequest,
    ChartDataRequest,
    ChartIndicatorBatchRequest,
    ChartIndicatorBatchResponse,
    ChartIndicatorSupportResponse,
    ChartRangePresetsResponse,
)
from app.services.chart_indicator_service import ChartIndicatorService, get_chart_indicator_service
from app.services.chart_service import (
    TIMEFRAME_DEFS,
    get_allowed_timeframes,
    get_chart_data,
    resolve_range_presets,
    resolve_request_dates,
)
from app.services.dataset_service import INDICATOR_CONFIGS
from app.utils.timestamps import now_ms_utc

router = APIRouter()
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────
@router.post("/indicators", response_model=ChartIndicatorBatchResponse)
async def chart_indicators(
    request: ChartIndicatorBatchRequest,
    service: ChartIndicatorService = Depends(get_chart_indicator_service),
) -> ChartIndicatorBatchResponse:
    """Compute indicators on the exact bar sequence already shown by a caller."""
    try:
        symbol, indicators = await asyncio.to_thread(
            service.compute,
            request.symbol,
            [bar.model_dump() for bar in request.bars],
            [indicator.model_dump() for indicator in request.indicators],
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ChartIndicatorBatchResponse.from_engine_result(symbol, indicators)


@router.get("/indicators/supported", response_model=ChartIndicatorSupportResponse)
async def supported_chart_indicators(
    service: ChartIndicatorService = Depends(get_chart_indicator_service),
) -> ChartIndicatorSupportResponse:
    """Return only indicators proven compatible with caller-owned chart bars."""
    return ChartIndicatorSupportResponse(names=service.supported_names())


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
    - PROVIDER_UNREACHABLE: a lake gap needed Polygon and Polygon could not be reached
    - INTERNAL_ERROR: unexpected failure
    """
    try:
        # Convert indicators to dict format
        indicator_dicts = [{"name": ind.name, "params": ind.params} for ind in request.indicators]

        # Numeric window authority (PRD §12): start_ms_utc/end_ms_utc take
        # per-field precedence over the date strings, resolved to inclusive
        # UTC calendar dates — the inverse of the frontend's utcMsToIsoDate.
        try:
            from_date, to_date = resolve_request_dates(
                request.from_date,
                request.to_date,
                request.start_ms_utc,
                request.end_ms_utc,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "INVALID_RANGE", "detail": str(exc)},
            ) from exc

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
            from_date=from_date,
            to_date=to_date,
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
            elif error_code == "PROVIDER_UNREACHABLE":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
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


@router.get("/range-presets", response_model=ChartRangePresetsResponse)
async def range_presets(session: Literal["rth", "extended"] = "rth") -> ChartRangePresetsResponse:
    """Calendar-resolved quick ranges ("last N trading sessions") for chart scope UIs.

    Every preset's start/end is computed by the canonical NYSE calendar —
    weekends, holidays, and the forming session are handled server-side, so
    the client applies the ms anchors verbatim and computes nothing. The
    ``session`` argument shapes only the bar estimates; any other value is
    rejected by validation rather than silently taking a branch.
    """
    try:
        result = await asyncio.to_thread(resolve_range_presets, now_ms_utc(), session=session)
        return ChartRangePresetsResponse(presets=result)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "INVALID_RANGE", "detail": str(e)},
        ) from e
    except Exception as e:
        logger.error(f"[CHART] Range presets error: {e}", exc_info=True)
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
