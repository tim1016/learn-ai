"""Indicator Reliability API router.

Endpoint for analyzing the predictive power of technical indicators
using Information Coefficient (IC) with proper out-of-sample validation,
multiple testing correction, and random baseline comparison.

IMPORTANT: This computes TIME-SERIES IC for a single asset.
This is NOT cross-sectional factor IC used in equity factor models.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.models.indicator_reliability_models import (
    DecayCurvePoint,
    HorizonICResult,
    IndicatorInfo,
    IndicatorReliabilityRequest,
    IndicatorReliabilityResponse,
    RegimeICPoint,
    RegimeResults,
    VerdictModel,
)
from app.research.indicator_reliability import (
    DEFAULT_VOL_WINDOW,
    MAX_DECAY_HORIZON,
    RANDOM_SIMULATIONS,
    TRADEABILITY_CAVEAT,
    TRAIN_RATIO,
    HorizonICAnalysis,
    bars_per_year,
    compute_ic_decay_curve,
    compute_indicator_reliability_with_oos,
    compute_ir_proxy,
    compute_regime_ic,
    compute_slope_decisions,
    compute_tradeability,
    find_best_horizon,
    format_indicator_display_name,
    generate_info_footnotes,
    generate_next_steps,
    generate_warnings,
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


def _to_horizon_ic_result(
    r: HorizonICAnalysis,
    include_distribution: bool = False,
) -> HorizonICResult:
    """Map the dataclass (internal) to the Pydantic model (wire format).

    ``include_distribution`` gates the 100-float random-baseline histogram
    — typically only the best horizon sends it, to keep payload compact.
    """
    return HorizonICResult(
        horizon=r.horizon,
        is_mean_ic=r.is_mean_ic,
        is_t_stat=r.is_t_stat,
        is_p_value=r.is_p_value,
        is_nw_t_stat=r.is_nw_t_stat,
        is_nw_p_value=r.is_nw_p_value,
        is_effective_n=r.is_effective_n,
        oos_mean_ic=r.oos_mean_ic,
        oos_t_stat=r.oos_t_stat,
        oos_p_value=r.oos_p_value,
        oos_effective_n=r.oos_effective_n,
        oos_retention=r.oos_retention,
        bonferroni_p=r.bonferroni_p,
        fdr_p=r.fdr_p,
        random_baseline_mean=r.random_baseline_mean,
        random_baseline_std=r.random_baseline_std,
        ic_vs_random_zscore=r.ic_vs_random_zscore,
        is_interpretation=r.is_interpretation,
        oos_interpretation=r.oos_interpretation,
        is_hit_rate=r.is_hit_rate,
        is_daily_ic_std=r.is_daily_ic_std,
        strength_label=r.strength_label,
        stability_label=r.stability_label,
        direction_label=r.direction_label,
        retention_delta_pct=r.retention_delta_pct,
        slope_adds_value=r.slope_adds_value,
        slope_recommended=r.slope_recommended,
        annualized_ir=r.annualized_ir,
        sharpe_estimate=r.sharpe_estimate,
        breadth_per_year=r.breadth_per_year,
        random_baseline_distribution=(
            r.random_baseline_distribution if include_distribution else []
        ),
    )


def _build_verdict(
    raw_results: list[HorizonICAnalysis],
    best_horizon: int | None,
) -> VerdictModel | None:
    """Compute top-line verdict from best horizon, or max-|IC| fallback.

    Tradeability is derived from the Sharpe proxy already populated on the
    HorizonICAnalysis (see router main flow).
    """
    if not raw_results:
        return None

    if best_horizon is not None:
        picked = next((r for r in raw_results if r.horizon == best_horizon), None)
    else:
        picked = max(raw_results, key=lambda r: abs(r.is_mean_ic))

    if picked is None:
        return None

    tradeability = compute_tradeability(picked.sharpe_estimate, picked.stability_label)

    return VerdictModel(
        direction=picked.direction_label,  # type: ignore[arg-type]
        strength=picked.strength_label,  # type: ignore[arg-type]
        stability=picked.stability_label,  # type: ignore[arg-type]
        tradeability=tradeability,  # type: ignore[arg-type]
        horizon=picked.horizon,
        tradeability_caveat=TRADEABILITY_CAVEAT,
    )


@router.post("/indicator-reliability", response_model=IndicatorReliabilityResponse)
async def calculate_indicator_reliability(
    request: IndicatorReliabilityRequest,
) -> IndicatorReliabilityResponse:
    """Analyze indicator reliability using IC with OOS validation.

    This endpoint:
    1. Fetches OHLCV bars for the ticker and date range
    2. Calculates the specified indicator using pandas-ta
    3. Splits data into 70% train / 30% test
    4. Computes IC on both splits for each horizon
    5. Applies Bonferroni and FDR multiple testing corrections
    6. Compares against random baseline (100 shuffled signals)
    7. Returns warnings about overfit risk and data issues

    IMPORTANT: This is time-series IC for a single asset, NOT cross-sectional factor IC.
    """
    try:
        logger.info(
            "[Reliability] Request: %s %s(%s) [%s -> %s] horizons=%s",
            request.symbol,
            request.indicator_name,
            request.indicator_params,
            request.from_date,
            request.to_date,
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
            request.from_date,
            max_lookback,
            request.timespan,
            request.multiplier,
        )

        # Fetch bars (including warmup)
        bars = fetch_bars_chunked(
            _polygon_client,
            ticker=request.symbol,
            from_date=warmup_start,
            to_date=request.to_date,
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
        from_ts = int(pd.Timestamp(request.from_date).timestamp() * 1000)
        df = df[df["timestamp"] >= from_ts].reset_index(drop=True)

        if len(df) < 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient data after warmup trim: {len(df)} bars",
            )

        logger.info("[Reliability] Bars after trim: %d", len(df))

        # Compute IC with OOS validation
        raw_results, slope_results, metadata = compute_indicator_reliability_with_oos(
            df=df,
            indicator_column=indicator_column,
            horizons=request.horizons,
            include_slope=request.include_slope,
            train_ratio=TRAIN_RATIO,
            random_simulations=RANDOM_SIMULATIONS,
        )

        # Populate IR proxy on every HorizonICAnalysis (cheap arithmetic).
        bpy = bars_per_year(request.timespan, request.multiplier)
        for r in raw_results:
            ir, sharpe, breadth = compute_ir_proxy(r.is_mean_ic, r.horizon, bpy)
            r.annualized_ir = ir
            r.sharpe_estimate = sharpe
            r.breadth_per_year = breadth
        if slope_results:
            for s in slope_results:
                ir, sharpe, breadth = compute_ir_proxy(s.is_mean_ic, s.horizon, bpy)
                s.annualized_ir = ir
                s.sharpe_estimate = sharpe
                s.breadth_per_year = breadth

        # Train split for diagnostic computations (decay curve, regime IC).
        # Mirrors the chronological split used inside compute_..._with_oos.
        split_idx = int(len(df) * TRAIN_RATIO)
        train_df = df.iloc[:split_idx].reset_index(drop=True)

        # IC decay curve (continuous horizons, IS only)
        decay_max = min(max(request.horizons) + 10, MAX_DECAY_HORIZON)
        decay_points = compute_ic_decay_curve(train_df, indicator_column, decay_max)
        decay_response = [
            DecayCurvePoint(
                horizon=p.horizon,
                ic=p.ic,
                p_value=p.p_value,
                ic_stderr=p.ic_stderr,
            )
            for p in decay_points
        ]

        # Regime-conditioned IC (volatility high/low, IS only)
        regime_dict = compute_regime_ic(
            train_df, indicator_column, request.horizons, window=DEFAULT_VOL_WINDOW
        )
        regime_response = RegimeResults(
            high_vol=[
                RegimeICPoint(
                    horizon=p.horizon,
                    mean_ic=p.mean_ic,
                    t_stat=p.t_stat,
                    p_value=p.p_value,
                    effective_n=p.effective_n,
                    hit_rate=p.hit_rate,
                    bars_in_regime=p.bars_in_regime,
                )
                for p in (regime_dict["high_vol"] or [])
            ]
            or None,
            low_vol=[
                RegimeICPoint(
                    horizon=p.horizon,
                    mean_ic=p.mean_ic,
                    t_stat=p.t_stat,
                    p_value=p.p_value,
                    effective_n=p.effective_n,
                    hit_rate=p.hit_rate,
                    bars_in_regime=p.bars_in_regime,
                )
                for p in (regime_dict["low_vol"] or [])
            ]
            or None,
            vol_window=DEFAULT_VOL_WINDOW,
        )

        # Pair raw + slope by horizon and compute slope decision flags in-place
        # on the slope dataclasses before mapping to the wire format.
        if slope_results:
            raw_by_horizon = {r.horizon: r for r in raw_results}
            for s in slope_results:
                raw_counterpart = raw_by_horizon.get(s.horizon)
                if raw_counterpart is not None:
                    adds, recommended = compute_slope_decisions(raw_counterpart, s)
                    s.slope_adds_value = adds
                    s.slope_recommended = recommended

        # Find best horizon first so we can gate the baseline distribution payload.
        best_horizon = find_best_horizon(raw_results)

        results = [
            _to_horizon_ic_result(r, include_distribution=(r.horizon == best_horizon))
            for r in raw_results
        ]
        slope_response = (
            [_to_horizon_ic_result(r) for r in slope_results] if slope_results else None
        )

        # Get best horizon's daily IC series for the chart
        daily_ic_values: list[float] = []
        daily_ic_dates: list[str] = []

        if best_horizon is not None:
            for r in raw_results:
                if r.horizon == best_horizon:
                    daily_ic_values = r.is_daily_ic_values
                    daily_ic_dates = r.is_daily_ic_dates
                    break
        elif raw_results:
            # Fall back to first horizon
            daily_ic_values = raw_results[0].is_daily_ic_values
            daily_ic_dates = raw_results[0].is_daily_ic_dates

        # Multiple testing summary
        any_bonferroni = any(r.bonferroni_p < 0.05 for r in raw_results)
        any_fdr = any(r.fdr_p < 0.05 for r in raw_results)

        # Generate warnings
        warnings = generate_warnings(
            results=raw_results,
            test_bars=metadata.get("test_bars", 0),
            num_horizons=len(request.horizons),
        )

        verdict = _build_verdict(raw_results, best_horizon)
        next_steps = generate_next_steps(
            raw_results, slope_results, regime_dict, best_horizon
        )
        info_footnotes = generate_info_footnotes(request.horizons)

        return IndicatorReliabilityResponse(
            success=True,
            ticker=request.symbol,
            indicator_name=request.indicator_name,
            indicator_params=request.indicator_params,
            display_name=format_indicator_display_name(
                request.indicator_name,
                request.indicator_params,
            ),
            category=get_indicator_category(request.indicator_name),
            start_date=request.from_date,
            end_date=request.to_date,
            bar_count=len(df),
            train_start=metadata.get("train_start"),
            train_end=metadata.get("train_end"),
            test_start=metadata.get("test_start"),
            test_end=metadata.get("test_end"),
            train_bars=metadata.get("train_bars"),
            test_bars=metadata.get("test_bars"),
            train_ratio=TRAIN_RATIO,
            results=results,
            slope_results=slope_response,
            daily_ic_values=daily_ic_values,
            daily_ic_dates=daily_ic_dates,
            best_horizon=best_horizon,
            any_significant_after_bonferroni=any_bonferroni,
            any_significant_after_fdr=any_fdr,
            num_horizons_tested=len(request.horizons),
            random_simulations=RANDOM_SIMULATIONS,
            verdict=verdict,
            decay_curve=decay_response,
            regime_results=regime_response,
            next_steps=next_steps,
            info_footnotes=info_footnotes,
            warnings=warnings,
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
