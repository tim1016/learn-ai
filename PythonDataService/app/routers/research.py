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
    BuildIvHistoryRequest,
    BuildIvHistoryResponse,
    CostViabilityResponse,
    CrossSectionalReportResponse,
    DataSufficiencyResponse,
    DeflatedSharpeResponse,
    EffectiveSampleSizeResponse,
    FeatureInfoResponse,
    FeatureStageCriterionResponse,
    FeatureStageInfoResponse,
    FeatureValidationSpecResponse,
    FeatureValidationVerdictResponse,
    GraduationCriterionResponse,
    GraduationResultResponse,
    GraduationStageInfoResponse,
    IcCiResponse,
    IvDiagnosticsReportResponse,
    MethodologyResponse,
    MonthlyICBreakdownResponse,
    MultipleTestingWarningResponse,
    ParameterStabilityResponse,
    QuantileBinResponse,
    RegimeBucketResponse,
    RegimeICResponse,
    RobustnessResponse,
    RollingTStatPointResponse,
    RunBatchOptionsRequest,
    RunFeatureResearchRequest,
    RunFeatureResearchResponse,
    RunOptionsFeatureResearchRequest,
    RunSignalEngineRequest,
    RunSignalEngineResponse,
    SharpeCiResponse,
    SignalBehaviorMetricsResponse,
    SignalDiagnosticsResponse,
    Stage0FailureResponse,
    Stage0RejectionResponse,
    StageAdvanceCriterionResponse,
    StructuralBreakPointResponse,
    TargetMetadataResponse,
    TickerBatchResult,
    TrainTestSplitResponse,
    ValidationScreenResponse,
    WalkForwardResultResponse,
    WalkForwardWindowResponse,
)
from app.research.batch_runner import run_cross_sectional_study
from app.research.config import ResearchConfig
from app.research.documentation.formulas import get_all_documentation
from app.research.feature_spec import FeatureValidationSpec
from app.research.feature_validation import (
    FeatureValidationVerdict,
    ValidationScreen,
)
from app.research.features.registry import get_feature_metadata, list_available_features
from app.research.options.diagnostics import run_iv_diagnostics
from app.research.options.iv_builder import build_iv_history
from app.research.options_runner import run_options_feature_research
from app.research.runner import run_feature_research
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine
from app.research.target import TargetResult
from app.services.polygon_client import PolygonClientService

router = APIRouter()
logger = logging.getLogger(__name__)


def _map_screen(screen: ValidationScreen) -> ValidationScreenResponse:
    return ValidationScreenResponse(
        name=screen.name,
        description=screen.description,
        passed=screen.passed,
        required_for_stage1=screen.required_for_stage1,
        failure_reasons=list(screen.failure_reasons),
    )


def _map_feature_spec(spec: FeatureValidationSpec) -> FeatureValidationSpecResponse:
    return FeatureValidationSpecResponse(
        feature_name=spec.feature_name,
        default_target=spec.default_target,
        expected_direction=spec.expected_direction,
        expected_shape=spec.expected_shape,
        stationarity_required=spec.stationarity_required,
        monotonicity_required=spec.monotonicity_required,
        is_signed_target_appropriate=spec.is_signed_target_appropriate,
        intent=spec.intent,
        notes=list(spec.notes),
    )


def _map_target_metadata(target: TargetResult) -> TargetMetadataResponse:
    return TargetMetadataResponse(
        target_name=target.target_name,
        horizon_minutes=target.horizon_minutes,
        horizon_bars=target.horizon_bars,
        bar_minutes=target.bar_minutes,
        timezone=target.timezone,
        valid_count=target.valid_count,
        total_count=target.total_count,
        valid_ratio=target.valid_ratio,
        invalid_reason_counts=dict(target.invalid_reason_counts),
    )


def _map_validation_verdict(
    verdict: FeatureValidationVerdict,
) -> FeatureValidationVerdictResponse:
    return FeatureValidationVerdictResponse(
        statistical_screen=_map_screen(verdict.statistical_screen),
        economic_screen=_map_screen(verdict.economic_screen),
        oos_screen=_map_screen(verdict.oos_screen),
        multiple_testing_screen=_map_screen(verdict.multiple_testing_screen),
        regime_stability_screen=_map_screen(verdict.regime_stability_screen),
        multiple_testing=MultipleTestingWarningResponse(
            raw_nw_p_value=verdict.multiple_testing.raw_nw_p_value,
            holm_p_value=verdict.multiple_testing.holm_p_value,
            n_family=verdict.multiple_testing.n_family,
            note=verdict.multiple_testing.note,
        ),
        cost_viability=CostViabilityResponse(
            gross_spread_bps_signed=verdict.cost_viability.gross_spread_bps_signed,
            directional_spread_bps=verdict.cost_viability.directional_spread_bps,
            cost_assumption_one_way_bps=verdict.cost_viability.cost_assumption_one_way_bps,
            cost_erasure_one_way_bps=verdict.cost_viability.cost_erasure_one_way_bps,
            net_spread_bps_at_assumption=verdict.cost_viability.net_spread_bps_at_assumption,
            viable_at_assumption=verdict.cost_viability.viable_at_assumption,
            spec_direction=verdict.cost_viability.spec_direction,
            note=verdict.cost_viability.note,
        ),
        direction_matches_spec=verdict.direction_matches_spec,
        target_signed_appropriate=verdict.target_signed_appropriate,
        ic_ci=IcCiResponse(
            point=verdict.ic_ci.point,
            se=verdict.ic_ci.se,
            ci_lower=verdict.ic_ci.ci_lower,
            ci_upper=verdict.ic_ci.ci_upper,
            confidence_level=verdict.ic_ci.confidence_level,
            n_eff_used=verdict.ic_ci.n_eff_used,
            valid=verdict.ic_ci.valid,
            se_approximation_note=verdict.ic_ci.se_approximation_note,
        ),
        stage_info=FeatureStageInfoResponse(
            stage=verdict.stage_info.stage,
            label=verdict.stage_info.label,
            description=verdict.stage_info.description,
            next_stage_label=verdict.stage_info.next_stage_label,
            advance_criteria=[
                FeatureStageCriterionResponse(
                    name=c.name,
                    description=c.description,
                    current_value=c.current_value,
                    required_repr=c.required_repr,
                    met=c.met,
                )
                for c in verdict.stage_info.advance_criteria
            ],
            failed_screens=list(verdict.stage_info.failed_screens),
        ),
        final_decision=verdict.final_decision,
    )


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
                        month=r.month,
                        t_stat_smoothed=r.t_stat_smoothed,
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
                )
                if rob.train_test
                else None,
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
            quantile_bins=[QuantileBinResponse(**bin_dict) for bin_dict in report.quantile_bins],
            is_monotonic=report.is_monotonic,
            monotonicity_ratio=report.monotonicity_ratio,
            ic_values=report.ic_values,
            ic_dates=report.ic_dates,
            robustness=robustness_response,
            feature_spec=(
                _map_feature_spec(report.feature_spec)
                if report.feature_spec is not None
                else None
            ),
            target_metadata=(
                _map_target_metadata(report.target)
                if report.target is not None
                else None
            ),
            validation_verdict=(
                _map_validation_verdict(report.validation_verdict)
                if report.validation_verdict is not None
                else None
            ),
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
                    n_folds_used=wf.alpha_decay.n_folds_used,
                    is_test_valid=wf.alpha_decay.is_test_valid,
                    is_significant=wf.alpha_decay.is_significant,
                )
                if wf.alpha_decay
                else None,
            )

        # Map graduation (criteria + Stage 0 + ladder)
        graduation_response = None
        if report.graduation:
            g = report.graduation
            stage0_response = Stage0RejectionResponse(
                rejected=g.stage0_rejection.rejected,
                failed_criteria=[
                    Stage0FailureResponse(
                        criterion_name=f.criterion_name,
                        value=f.value,
                        threshold_repr=f.threshold_repr,
                        message=f.message,
                    )
                    for f in g.stage0_rejection.failed_criteria
                ],
            )
            stage_info_response = GraduationStageInfoResponse(
                stage=g.stage_info.stage,
                label=g.stage_info.label,
                description=g.stage_info.description,
                next_stage_label=g.stage_info.next_stage_label,
                advance_criteria=[
                    StageAdvanceCriterionResponse(
                        name=ac.name,
                        description=ac.description,
                        current_value=ac.current_value,
                        required_repr=ac.required_repr,
                        met=ac.met,
                    )
                    for ac in g.stage_info.advance_criteria
                ],
            )
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
                )
                if g.parameter_stability
                else None,
                stage0_rejection=stage0_response,
                stage_info=stage_info_response,
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

        # Map Sharpe CI on combined OOS Sharpe
        oos_sharpe_ci_response = None
        if report.oos_sharpe_ci is not None:
            ci = report.oos_sharpe_ci
            oos_sharpe_ci_response = SharpeCiResponse(
                point=ci.point,
                se=ci.se,
                ci_lower=ci.ci_lower,
                ci_upper=ci.ci_upper,
                confidence_level=ci.confidence_level,
                n_eff_used=ci.n_eff_used,
                valid=ci.valid,
            )

        # Map Deflated Sharpe on the IS grid headline
        deflated_sharpe_response = None
        if report.deflated_sharpe is not None:
            ds = report.deflated_sharpe
            deflated_sharpe_response = DeflatedSharpeResponse(
                raw_sharpe=ds.raw_sharpe,
                expected_max_under_null=ds.expected_max_under_null,
                dsr_probability=ds.dsr_probability,
                n_trials=ds.n_trials,
                skewness=ds.skewness,
                kurtosis=ds.kurtosis,
                valid=ds.valid,
            )

        # Map joint regime coverage with effective-trades estimates
        joint_regime_buckets = [
            RegimeBucketResponse(
                vol_label=b.vol_label,
                trend_label=b.trend_label,
                days=b.days,
                effective_trades=b.effective_trades,
                badge=b.badge,
            )
            for b in report.joint_regime_coverage
        ]

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
            joint_regime_coverage=joint_regime_buckets,
            signal_behavior=sb_response,
            oos_sharpe_ci=oos_sharpe_ci_response,
            deflated_sharpe=deflated_sharpe_response,
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


@router.post("/build-iv-history", response_model=BuildIvHistoryResponse)
async def build_iv_history_endpoint(
    request: BuildIvHistoryRequest,
) -> BuildIvHistoryResponse:
    """Build historical 30-day constant-maturity IV for a ticker.

    Long-running: may take 5-10 minutes per ticker due to API calls.
    Derives IV from expired options contracts + Black-Scholes inversion.
    """
    try:
        logger.info(
            "[IV Builder] Request: %s [%s → %s]",
            request.underlying_ticker,
            request.start_date,
            request.end_date,
        )

        polygon_client = PolygonClientService()

        iv_df = build_iv_history(
            underlying=request.underlying_ticker,
            start_date=request.start_date,
            end_date=request.end_date,
            polygon_client=polygon_client,
        )

        if iv_df.empty:
            return BuildIvHistoryResponse(
                success=False,
                underlying_ticker=request.underlying_ticker,
                start_date=request.start_date,
                end_date=request.end_date,
                error="No IV data could be derived",
            )

        # Run diagnostics
        diag = run_iv_diagnostics(iv_df)
        diag_response = IvDiagnosticsReportResponse(
            valid=diag.valid,
            missing_pct=diag.missing_pct,
            total_trading_days=diag.total_trading_days,
            valid_iv_days=diag.valid_iv_days,
            first_date=diag.first_date,
            last_date=diag.last_date,
            gaps=diag.gaps,
            dte_spikes=diag.dte_spikes,
            iv_mean=diag.iv_mean,
            iv_std=diag.iv_std,
            iv_min=diag.iv_min,
            iv_max=diag.iv_max,
            iv_skewness=diag.iv_skewness,
            discontinuities=diag.discontinuities,
            warnings=diag.warnings,
        )

        iv_records = iv_df.to_dict(orient="records")

        return BuildIvHistoryResponse(
            success=True,
            underlying_ticker=request.underlying_ticker,
            start_date=request.start_date,
            end_date=request.end_date,
            data_points=len(iv_records),
            iv_data=iv_records,
            diagnostics=diag_response,
        )

    except Exception as e:
        logger.error("[IV Builder] Endpoint error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"IV history build failed: {e}",
        )


@router.post("/run-options-feature", response_model=RunFeatureResearchResponse)
async def run_options_feature_endpoint(
    request: RunOptionsFeatureResearchRequest,
) -> RunFeatureResearchResponse:
    """Run options feature research using derived IV data.

    Uses the same IC analysis pipeline as stock features but adapted for
    daily-frequency options signals.
    """
    try:
        logger.info(
            "[Options Research] Request: %s %s (%d IV points)",
            request.ticker,
            request.feature_name,
            len(request.iv_data),
        )

        iv_dicts = [iv.model_dump() for iv in request.iv_data]
        bar_dicts = [bar.model_dump() for bar in request.stock_daily_bars]

        report = run_options_feature_research(
            ticker=request.ticker,
            feature_name=request.feature_name,
            iv_data=iv_dicts,
            stock_daily_bars=bar_dicts,
            start_date=request.start_date,
            end_date=request.end_date,
            target_type=request.target_type,
        )

        # Map robustness (same mapping as run-feature)
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
                        month=r.month,
                        t_stat_smoothed=r.t_stat_smoothed,
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
                )
                if rob.train_test
                else None,
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
            quantile_bins=[QuantileBinResponse(**bin_dict) for bin_dict in report.quantile_bins],
            is_monotonic=report.is_monotonic,
            monotonicity_ratio=report.monotonicity_ratio,
            ic_values=report.ic_values,
            ic_dates=report.ic_dates,
            robustness=robustness_response,
            error=report.error,
        )

    except Exception as e:
        logger.error("[Options Research] Endpoint error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Options feature research failed: {e}",
        )


@router.post("/run-batch-options", response_model=CrossSectionalReportResponse)
async def run_batch_options_endpoint(
    request: RunBatchOptionsRequest,
) -> CrossSectionalReportResponse:
    """Run cross-sectional options feature study across multiple tickers.

    Tests the same options feature across a universe of tickers and
    determines if the effect is cross-sectionally consistent.
    """
    try:
        logger.info(
            "[Batch Options] Request: %s across %d tickers",
            request.feature_name,
            len(request.tickers),
        )

        polygon_client = PolygonClientService()

        report = run_cross_sectional_study(
            feature_name=request.feature_name,
            tickers=request.tickers,
            start_date=request.start_date,
            end_date=request.end_date,
            polygon_client=polygon_client,
            target_type=request.target_type,
        )

        ticker_results = [
            TickerBatchResult(
                ticker=tr["ticker"],
                mean_ic=tr.get("mean_ic", 0.0),
                ic_t_stat=tr.get("ic_t_stat", 0.0),
                ic_p_value=tr.get("ic_p_value", 1.0),
                nw_t_stat=tr.get("nw_t_stat", 0.0),
                nw_p_value=tr.get("nw_p_value", 1.0),
                effective_n=tr.get("effective_n", 0.0),
                is_stationary=tr.get("is_stationary", False),
                passed_validation=tr.get("passed_validation", False),
                data_points=tr.get("data_points", 0),
                error=tr.get("error"),
            )
            for tr in report.ticker_results
        ]

        return CrossSectionalReportResponse(
            success=True,
            feature_name=report.feature_name,
            tickers_tested=report.tickers_tested,
            tickers_passed=report.tickers_passed,
            pass_rate=report.pass_rate,
            cross_sectional_consistent=report.cross_sectional_consistent,
            aggregate_ic=report.aggregate_ic,
            ticker_results=ticker_results,
            summary=report.summary,
        )

    except Exception as e:
        logger.error("[Batch Options] Endpoint error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Batch options research failed: {e}",
        )
