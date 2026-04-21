"""Indicator Reliability API router.

Endpoint for analyzing the predictive power of technical indicators
using Information Coefficient (IC) across multiple forward horizons.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.models.indicator_reliability_models import (
    HorizonICResult,
    IndicatorInfo,
    IndicatorReliabilityRequest,
    IndicatorReliabilityResponse,
)
from app.research.indicator_reliability import (
    compute_forward_return,
    compute_indicator_reliability,
    find_best_horizon,
    format_indicator_display_name,
    get_indicator_category,
)
from app.services.dataset_service import (
    calculate_dynamic_indicators,
    compute_warmup_start_date,
    fetch_bars_chunked,
    filter_session,
    get_indicator_configs,
    list_available_indicators,
)
from app.services.polygon_client import PolygonClientService

router = APIRouter()
logger = logging.getLogger(__name__)

# Module-level singleton
_polygon_client = PolygonClientService()


def _estimate_max_lookback(params: dict[str, Any]) -> int:
    """Estimate indicator warmup period from params."""
    lookback = 200  # Default
    for key in ("length", "slow", "k", "bb_length", "kc_length", "lower_length", "upper_length"):
        val = params.get(key, 0)
        if isinstance(val, (int, float)):
            lookback = max(lookback, int(val))
    return lookback


@router.post("/indicator-reliability", response_model=IndicatorReliabilityResponse)
async def calculate_indicator_reliability(
    request: IndicatorReliabilityRequest,
) -> IndicatorReliabilityResponse:
    """Analyze indicator reliability using IC across forward horizons.

    This endpoint:
    1. Fetches OHLCV bars for the ticker and date range
    2. Calculates the specified indicator using pandas-ta
    3. Computes IC between indicator values and forward returns for each horizon
    4. Returns statistical metrics and interpretations per horizon
    """
    try:
        logger.info(
            "[Reliability] Request: %s %s(%s) [%s → %s] horizons=%s",
            request.ticker,
            request.indicator_name,
            request.indicator_params,
            request.start_date,
            request.end_date,
            request.horizons,
        )

        # Validate horizons
        if not request.horizons:
            request.horizons = [1, 5, 10, 15, 30]
        if max(request.horizons) > 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Maximum horizon is 100 bars",
            )

        # Compute warmup period
        max_lookback = _estimate_max_lookback(request.indicator_params)
        warmup_start = compute_warmup_start_date(
            request.start_date,
            max_lookback,
            request.timespan,
            request.multiplier,
        )

        # Fetch bars (including warmup)
        bars = fetch_bars_chunked(
            _polygon_client,
            ticker=request.ticker,
            from_date=warmup_start,
            to_date=request.end_date,
            timespan=request.timespan,
            multiplier=request.multiplier,
            adjusted=True,
        )

        if len(bars) < max_lookback + max(request.horizons):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient data: got {len(bars)} bars, need at least {max_lookback + max(request.horizons)}",
            )

        logger.info("[Reliability] Fetched %d bars (including warmup)", len(bars))

        # Convert to DataFrame and filter session
        import pandas as pd

        df = pd.DataFrame(bars)
        df = filter_session(df, "rth")

        # Calculate the indicator
        indicator_entry = {
            "name": request.indicator_name,
            "params": request.indicator_params,
        }
        df, column_meta = calculate_dynamic_indicators(df, [indicator_entry])

        if not column_meta:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Indicator '{request.indicator_name}' could not be calculated",
            )

        # Find the primary indicator column (first one)
        indicator_column = column_meta[0]["column"]
        logger.info("[Reliability] Indicator column: %s", indicator_column)

        # Trim warmup period
        from_ts = int(pd.Timestamp(request.start_date).timestamp() * 1000)
        df = df[df["timestamp"] >= from_ts].reset_index(drop=True)

        if len(df) < 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient data after warmup trim: {len(df)} bars",
            )

        logger.info("[Reliability] Bars after trim: %d", len(df))

        # Compute IC across horizons
        raw_results, slope_results = compute_indicator_reliability(
            df=df,
            indicator_column=indicator_column,
            horizons=request.horizons,
            include_slope=request.include_slope,
        )

        # Convert to response models
        results = [
            HorizonICResult(
                horizon=r.horizon,
                mean_ic=r.mean_ic,
                t_stat=r.t_stat,
                p_value=r.p_value,
                nw_t_stat=r.nw_t_stat,
                nw_p_value=r.nw_p_value,
                effective_n=r.effective_n,
                interpretation=r.interpretation,
            )
            for r in raw_results
        ]

        slope_response = None
        if slope_results:
            slope_response = [
                HorizonICResult(
                    horizon=r.horizon,
                    mean_ic=r.mean_ic,
                    t_stat=r.t_stat,
                    p_value=r.p_value,
                    nw_t_stat=r.nw_t_stat,
                    nw_p_value=r.nw_p_value,
                    effective_n=r.effective_n,
                    interpretation=r.interpretation,
                )
                for r in slope_results
            ]

        # Find best horizon and get its daily IC series
        best_horizon = find_best_horizon(raw_results)
        daily_ic_values: list[float] = []
        daily_ic_dates: list[str] = []

        if best_horizon is not None:
            for r in raw_results:
                if r.horizon == best_horizon:
                    daily_ic_values = r.daily_ic_values
                    daily_ic_dates = r.daily_ic_dates
                    break
        elif raw_results:
            # Fall back to first horizon
            daily_ic_values = raw_results[0].daily_ic_values
            daily_ic_dates = raw_results[0].daily_ic_dates

        return IndicatorReliabilityResponse(
            success=True,
            ticker=request.ticker,
            indicator_name=request.indicator_name,
            indicator_params=request.indicator_params,
            display_name=format_indicator_display_name(
                request.indicator_name,
                request.indicator_params,
            ),
            category=get_indicator_category(request.indicator_name),
            start_date=request.start_date,
            end_date=request.end_date,
            bar_count=len(df),
            results=results,
            slope_results=slope_response,
            daily_ic_values=daily_ic_values,
            daily_ic_dates=daily_ic_dates,
            best_horizon=best_horizon,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[Reliability] Endpoint error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Indicator reliability analysis failed: {e}",
        )


@router.get("/indicators", response_model=dict[str, list[IndicatorInfo]])
async def list_indicators() -> dict[str, list[IndicatorInfo]]:
    """List all available pandas-ta indicators grouped by category.

    Returns indicators with their descriptions and configurable parameters.
    """
    try:
        # Get indicators grouped by category
        categories = list_available_indicators()

        # Get parameter configs
        param_configs = get_indicator_configs()

        result: dict[str, list[IndicatorInfo]] = {}

        for category, indicators in categories.items():
            result[category] = [
                IndicatorInfo(
                    name=ind["name"],
                    category=ind["category"],
                    description=ind["description"],
                    params=param_configs.get(ind["name"], []),
                )
                for ind in indicators
            ]

        return result

    except Exception as e:
        logger.error("[Reliability] List indicators error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list indicators: {e}",
        )


@router.get("/indicator-params/{indicator_name}")
async def get_indicator_params(indicator_name: str) -> list[dict]:
    """Get configurable parameters for a specific indicator."""
    try:
        param_configs = get_indicator_configs()
        params = param_configs.get(indicator_name.lower(), [])

        if not params:
            # Try to infer from pandas-ta function signature
            import inspect

            import pandas_ta as ta

            fn = getattr(ta, indicator_name.lower(), None)
            if fn is not None:
                sig = inspect.signature(fn)
                inferred = []
                for name, param in sig.parameters.items():
                    if name in ("close", "high", "low", "open_", "volume", "open"):
                        continue
                    if param.default != inspect.Parameter.empty and isinstance(
                        param.default, (int, float)
                    ):
                        inferred.append(
                            {
                                "name": name,
                                "type": "int" if isinstance(param.default, int) else "float",
                                "default": param.default,
                                "description": f"{name} parameter",
                            }
                        )
                return inferred

        return params

    except Exception as e:
        logger.error("[Reliability] Get params error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get indicator params: {e}",
        )
