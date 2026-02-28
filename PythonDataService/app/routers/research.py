"""Research Lab API router.

Endpoints for running feature validation experiments and retrieving
documentation metadata for the Angular information panel.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.research_models import (
    FeatureInfoResponse,
    MonthlyICBreakdownResponse,
    QuantileBinResponse,
    RegimeICResponse,
    RobustnessResponse,
    RollingTStatPointResponse,
    RunFeatureResearchRequest,
    RunFeatureResearchResponse,
    TrainTestSplitResponse,
)
from app.research.config import ResearchConfig
from app.research.documentation.formulas import get_all_documentation
from app.research.features.registry import get_feature_metadata, list_available_features
from app.research.runner import run_feature_research

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/run-feature", response_model=RunFeatureResearchResponse)
async def run_feature_research_endpoint(
    request: RunFeatureResearchRequest,
) -> RunFeatureResearchResponse:
    """Run a feature validation experiment.

    Accepts OHLCV bars, computes the requested feature, validates it
    against 15-minute forward log returns using IC, stationarity, and
    quantile analysis.
    """
    try:
        logger.info(
            "[Research] Request: %s %s (%d bars)",
            request.ticker,
            request.feature_name,
            len(request.bars),
        )

        bars_dicts = [bar.model_dump() for bar in request.bars]

        report = run_feature_research(
            ticker=request.ticker,
            feature_name=request.feature_name,
            bars=bars_dicts,
            start_date=request.start_date,
            end_date=request.end_date,
            config=ResearchConfig(),
        )

        # Map robustness results if available
        robustness_response = None
        if report.robustness is not None:
            rob = report.robustness
            robustness_response = RobustnessResponse(
                monthly_breakdown=[
                    MonthlyICBreakdownResponse(
                        month=m.month,
                        mean_ic=m.mean_ic,
                        t_stat=m.t_stat,
                        observation_count=m.observation_count,
                    )
                    for m in rob.monthly_breakdown
                ],
                pct_positive_months=rob.pct_positive_months,
                pct_significant_months=rob.pct_significant_months,
                best_month_ic=rob.best_month_ic,
                worst_month_ic=rob.worst_month_ic,
                stability_label=rob.stability_label,
                rolling_t_stat=[
                    RollingTStatPointResponse(
                        month=r.month, t_stat_smoothed=r.t_stat_smoothed,
                    )
                    for r in rob.rolling_t_stat
                ],
                volatility_regimes=[
                    RegimeICResponse(
                        regime_label=r.regime_label,
                        mean_ic=r.mean_ic,
                        t_stat=r.t_stat,
                        observation_count=r.observation_count,
                    )
                    for r in rob.volatility_regimes
                ],
                trend_regimes=[
                    RegimeICResponse(
                        regime_label=r.regime_label,
                        mean_ic=r.mean_ic,
                        t_stat=r.t_stat,
                        observation_count=r.observation_count,
                    )
                    for r in rob.trend_regimes
                ],
                train_test=TrainTestSplitResponse(
                    train_start=rob.train_test.train_start,
                    train_end=rob.train_test.train_end,
                    test_start=rob.train_test.test_start,
                    test_end=rob.train_test.test_end,
                    train_mean_ic=rob.train_test.train_mean_ic,
                    train_t_stat=rob.train_test.train_t_stat,
                    train_days=rob.train_test.train_days,
                    test_mean_ic=rob.train_test.test_mean_ic,
                    test_t_stat=rob.train_test.test_t_stat,
                    test_days=rob.train_test.test_days,
                    overfit_flag=rob.train_test.overfit_flag,
                ) if rob.train_test else None,
            )

        return RunFeatureResearchResponse(
            success=report.error is None,
            ticker=report.ticker,
            feature_name=report.feature_name,
            start_date=report.start_date,
            end_date=report.end_date,
            bars_used=report.bars_used,
            mean_ic=report.mean_ic,
            ic_t_stat=report.ic_t_stat,
            ic_p_value=report.ic_p_value,
            adf_pvalue=report.adf_pvalue,
            kpss_pvalue=report.kpss_pvalue,
            is_stationary=report.is_stationary,
            passed_validation=report.passed_validation,
            quantile_bins=[
                QuantileBinResponse(**bin_dict) for bin_dict in report.quantile_bins
            ],
            is_monotonic=report.is_monotonic,
            monotonicity_ratio=report.monotonicity_ratio,
            ic_values=report.ic_values,
            ic_dates=report.ic_dates,
            robustness=robustness_response,
            error=report.error,
        )

    except Exception as e:
        logger.error("[Research] Endpoint error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Research execution failed: {e}",
        )


@router.get("/features")
async def list_features() -> list[FeatureInfoResponse]:
    """List all available research features with metadata."""
    result: list[FeatureInfoResponse] = []
    for name in list_available_features():
        meta = get_feature_metadata(name)
        if meta is not None:
            result.append(
                FeatureInfoResponse(
                    name=meta.name,
                    formula_latex=meta.formula_latex,
                    variables=meta.variables,
                    example=meta.example,
                    interpretation=meta.interpretation,
                    implementation_note=meta.implementation_note,
                    window=meta.window,
                    category=meta.category,
                )
            )
    return result


@router.get("/documentation")
async def get_documentation() -> dict:
    """Return complete mathematical documentation for the UI information panel."""
    return get_all_documentation()
