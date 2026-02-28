"""Research Lab API router.

Endpoints for running feature validation experiments and retrieving
documentation metadata for the Angular information panel.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.research_models import (
    AlphaDecayStatsResponse,
    BacktestResultResponse,
    DataSufficiencyResponse,
    EffectiveSampleSizeResponse,
    FeatureInfoResponse,
    GraduationCriterionResponse,
    GraduationResultResponse,
    MethodologyResponse,
    MonthlyICBreakdownResponse,
    ParameterStabilityResponse,
    QuantileBinResponse,
    RegimeICResponse,
    RobustnessResponse,
    RollingTStatPointResponse,
    RunFeatureResearchRequest,
    RunFeatureResearchResponse,
    RunSignalEngineRequest,
    RunSignalEngineResponse,
    SignalBehaviorMetricsResponse,
    SignalDiagnosticsResponse,
    StructuralBreakPointResponse,
    TrainTestSplitResponse,
    WalkForwardResultResponse,
    WalkForwardWindowResponse,
)
from app.research.config import ResearchConfig
from app.research.documentation.formulas import get_all_documentation
from app.research.features.registry import get_feature_metadata, list_available_features
from app.research.runner import run_feature_research
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine

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
                pct_sign_consistent_months=rob.pct_sign_consistent_months,
                sign_consistent_stability_label=rob.sign_consistent_stability_label,
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
                    oos_retention=rob.train_test.oos_retention,
                    oos_retention_label=rob.train_test.oos_retention_label,
                ) if rob.train_test else None,
                structural_breaks=[
                    StructuralBreakPointResponse(
                        date=b.date,
                        ic_before=b.ic_before,
                        ic_after=b.ic_after,
                        t_stat=b.t_stat,
                        significant=b.significant,
                    )
                    for b in rob.structural_breaks
                ],
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
            nw_t_stat=report.nw_t_stat,
            nw_p_value=report.nw_p_value,
            effective_n=report.effective_n,
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


@router.post("/run-signal", response_model=RunSignalEngineResponse)
async def run_signal_engine_endpoint(
    request: RunSignalEngineRequest,
) -> RunSignalEngineResponse:
    """Run the signal engine pipeline.

    Converts a validated feature into a tradable signal with
    standardization, regime gating, backtesting, walk-forward validation,
    and graduation assessment.
    """
    try:
        logger.info(
            "[Signal] Request: %s %s (%d bars)",
            request.ticker,
            request.feature_name,
            len(request.bars),
        )

        bars_dicts = [bar.model_dump() for bar in request.bars]
        config = SignalConfig(
            feature_name=request.feature_name,
            flip_sign=request.flip_sign,
            regime_gate_enabled=request.regime_gate_enabled,
        )

        report = run_signal_engine(
            ticker=request.ticker,
            feature_name=request.feature_name,
            bars=bars_dicts,
            start_date=request.start_date,
            end_date=request.end_date,
            config=config,
        )

        # Map backtest grid
        backtest_grid = [
            BacktestResultResponse(
                threshold=bt.threshold,
                cost_bps=bt.cost_bps,
                dates=bt.dates,
                cumulative_returns=bt.cumulative_returns,
                positions=bt.positions,
                gross_sharpe=bt.gross_sharpe,
                net_sharpe=bt.net_sharpe,
                max_drawdown=bt.max_drawdown,
                annualized_turnover=bt.annualized_turnover,
                avg_holding_bars=bt.avg_holding_bars,
                win_rate=bt.win_rate,
                avg_win_loss_ratio=bt.avg_win_loss_ratio,
                total_trades=bt.total_trades,
                net_total_return=bt.net_total_return,
                gross_total_return=bt.gross_total_return,
            )
            for bt in report.backtest_grid
        ]

        # Map walk-forward
        walk_forward_response = None
        if report.walk_forward and report.walk_forward.windows:
            wf = report.walk_forward
            walk_forward_response = WalkForwardResultResponse(
                windows=[
                    WalkForwardWindowResponse(
                        fold_index=w.fold_index,
                        train_start=w.train_start,
                        train_end=w.train_end,
                        test_start=w.test_start,
                        test_end=w.test_end,
                        train_bars=w.train_bars,
                        test_bars=w.test_bars,
                        mu=w.mu,
                        sigma=w.sigma,
                        best_threshold=w.best_threshold,
                        oos_net_sharpe=w.oos_net_sharpe,
                        oos_gross_sharpe=w.oos_gross_sharpe,
                        oos_max_drawdown=w.oos_max_drawdown,
                        oos_net_return=w.oos_net_return,
                        oos_win_rate=w.oos_win_rate,
                        oos_total_trades=w.oos_total_trades,
                        oos_dates=w.oos_dates,
                        oos_cumulative_returns=w.oos_cumulative_returns,
                    )
                    for w in wf.windows
                ],
                mean_oos_sharpe=wf.mean_oos_sharpe,
                std_oos_sharpe=wf.std_oos_sharpe,
                median_oos_sharpe=wf.median_oos_sharpe,
                pct_windows_profitable=wf.pct_windows_profitable,
                pct_windows_positive_sharpe=wf.pct_windows_positive_sharpe,
                worst_window_sharpe=wf.worst_window_sharpe,
                best_window_sharpe=wf.best_window_sharpe,
                total_oos_bars=wf.total_oos_bars,
                combined_oos_dates=wf.combined_oos_dates,
                combined_oos_cumulative_returns=wf.combined_oos_cumulative_returns,
                oos_sharpe_trend_slope=wf.oos_sharpe_trend_slope,
                alpha_decay=AlphaDecayStatsResponse(
                    slope=wf.alpha_decay.slope,
                    intercept=wf.alpha_decay.intercept,
                    t_stat=wf.alpha_decay.t_stat,
                    p_value=wf.alpha_decay.p_value,
                    r_squared=wf.alpha_decay.r_squared,
                ) if wf.alpha_decay else None,
            )

        # Map graduation
        graduation_response = None
        if report.graduation:
            g = report.graduation
            graduation_response = GraduationResultResponse(
                criteria=[
                    GraduationCriterionResponse(
                        name=c.name,
                        description=c.description,
                        passed=c.passed,
                        value=c.value,
                        threshold=c.threshold,
                        label=c.label,
                        failure_reason=c.failure_reason,
                    )
                    for c in g.criteria
                ],
                overall_passed=g.overall_passed,
                overall_grade=g.overall_grade,
                summary=g.summary,
                status_label=g.status_label,
                parameter_stability=ParameterStabilityResponse(
                    sharpe_values_by_threshold=g.parameter_stability.sharpe_values_by_threshold,
                    stability_score=g.parameter_stability.stability_score,
                    stability_label=g.parameter_stability.stability_label,
                ) if g.parameter_stability else None,
            )

        # Map diagnostics
        diag_response = None
        if report.signal_diagnostics:
            d = report.signal_diagnostics
            diag_response = SignalDiagnosticsResponse(
                signal_mean=d.signal_mean,
                signal_std=d.signal_std,
                pct_time_active=d.pct_time_active,
                avg_abs_signal=d.avg_abs_signal,
                pct_filtered_by_threshold=d.pct_filtered_by_threshold,
                pct_gated_by_regime=d.pct_gated_by_regime,
            )

        # Map data sufficiency
        ds_response = None
        if report.data_sufficiency:
            ds = report.data_sufficiency
            ds_response = DataSufficiencyResponse(
                total_bars=ds.total_bars,
                train_bars=ds.train_bars,
                test_bars=ds.test_bars,
                walk_forward_folds=ds.walk_forward_folds,
                effective_oos_bars=ds.effective_oos_bars,
                regimes_covered=ds.regimes_covered,
                regime_coverage=ds.regime_coverage,
                coverage_warnings=ds.coverage_warnings,
            )

        # Map effective sample
        es_response = None
        if report.effective_sample:
            es = report.effective_sample
            es_response = EffectiveSampleSizeResponse(
                raw_n=es.raw_n,
                effective_n=es.effective_n,
                autocorrelation_lag1=es.autocorrelation_lag1,
                independent_bets=es.independent_bets,
                max_lag_used=es.max_lag_used,
                rho_sum=es.rho_sum,
            )

        # Map signal behavior
        sb_response = None
        if report.signal_behavior:
            sb = report.signal_behavior
            sb_response = SignalBehaviorMetricsResponse(
                avg_forward_return_when_active=sb.avg_forward_return_when_active,
                skewness_active_returns=sb.skewness_active_returns,
                avg_win_return=sb.avg_win_return,
                avg_loss_return=sb.avg_loss_return,
                hit_rate=sb.hit_rate,
            )

        # Map methodology
        meth_response = None
        if report.methodology:
            m = report.methodology
            meth_response = MethodologyResponse(
                train_months=m["train_months"],
                test_months=m["test_months"],
                window_type=m["window_type"],
                optimization_target=m["optimization_target"],
                annualization_factor=m["annualization_factor"],
                bars_per_day=m["bars_per_day"],
                horizon=m["horizon"],
                default_cost_bps=m["default_cost_bps"],
                min_bars_for_signal=m["min_bars_for_signal"],
                flip_sign=m["flip_sign"],
                regime_gate_enabled=m["regime_gate_enabled"],
                thresholds=m["thresholds"],
                cost_bps_options=m["cost_bps_options"],
            )

        return RunSignalEngineResponse(
            success=report.error is None,
            ticker=report.ticker,
            feature_name=report.feature_name,
            start_date=report.start_date,
            end_date=report.end_date,
            bars_used=report.bars_used,
            flip_sign=report.flip_sign,
            thresholds_tested=report.thresholds_tested,
            cost_bps_options=report.cost_bps_options,
            best_threshold=report.best_threshold,
            best_cost_bps=report.best_cost_bps,
            backtest_grid=backtest_grid,
            walk_forward=walk_forward_response,
            graduation=graduation_response,
            signal_diagnostics=diag_response,
            data_sufficiency=ds_response,
            effective_sample=es_response,
            regime_coverage=report.regime_coverage,
            signal_behavior=sb_response,
            methodology=meth_response,
            research_log=report.research_log,
            error=report.error,
        )

    except Exception as e:
        logger.error("[Signal] Endpoint error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signal engine execution failed: {e}",
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
