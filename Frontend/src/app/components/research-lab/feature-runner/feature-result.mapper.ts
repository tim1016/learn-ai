import {
  ResearchResult,
  QuantileBin,
  Robustness,
  MonthlyICBreakdown,
  RollingTStatPoint,
  RegimeIC,
  TrainTestSplit,
  StructuralBreakPoint,
  FeatureValidationSpec,
  FeatureValidationVerdict,
  ValidationScreen,
  MultipleTestingWarning,
  CostViability,
  IcCi,
  FeatureStageInfo,
  FeatureStageCriterion,
} from '../../../services/research.service';
import { mapLakeStudyDataSource, type LakeDataSourceRaw } from '../../../shared/lake-study/lake-study';

/** Shape of the ``target`` payload emitted by ``_serialize_target`` in
 *  ``PythonDataService/app/routers/jobs.py``. Mirrors the view model's
 *  ``TargetMetadata`` so the mapper below can rename keys 1:1. */
interface FeatureResearchTargetRaw {
  target_name: string;
  horizon_minutes: number;
  horizon_bars: number;
  bar_minutes: number;
  timezone: string;
  valid_count: number;
  total_count: number;
  valid_ratio: number;
  invalid_reason_counts: { reason: string; count: number }[];
}

/** Snake-case shape returned by /api/jobs-internal/feature-research worker. */
export interface FeatureResearchJobResultRaw {
  success: boolean;
  ticker: string;
  feature_name: string;
  start_date: string;
  end_date: string;
  bars_used: number;
  mean_ic: number;
  ic_t_stat: number;
  ic_p_value: number;
  nw_t_stat: number;
  nw_p_value: number;
  effective_n: number;
  ic_values: number[];
  ic_dates: string[];
  adf_pvalue: number;
  kpss_pvalue: number;
  is_stationary: boolean;
  quantile_bins: unknown[];
  is_monotonic: boolean;
  monotonicity_ratio: number;
  robustness: unknown;
  feature_spec: unknown;
  validation_verdict: unknown;
  target?: FeatureResearchTargetRaw | null;
  data_source?: LakeDataSourceRaw | null;
  passed_validation: boolean;
  error: string | null;
}

/** Map snake_case worker payload → camelCase ResearchResult. */
export function toResearchResult(raw: FeatureResearchJobResultRaw): ResearchResult {
  return {
    success: raw.success,
    ticker: raw.ticker,
    featureName: raw.feature_name,
    startDate: raw.start_date,
    endDate: raw.end_date,
    barsUsed: raw.bars_used,
    meanIC: raw.mean_ic,
    icTStat: raw.ic_t_stat,
    icPValue: raw.ic_p_value,
    nwTStat: raw.nw_t_stat,
    nwPValue: raw.nw_p_value,
    effectiveN: raw.effective_n,
    icValues: raw.ic_values ?? [],
    icDates: raw.ic_dates ?? [],
    adfPvalue: raw.adf_pvalue,
    kpssPvalue: raw.kpss_pvalue,
    isStationary: raw.is_stationary,
    quantileBins: mapQuantileBins(raw.quantile_bins),
    isMonotonic: raw.is_monotonic,
    monotonicityRatio: raw.monotonicity_ratio,
    passedValidation: raw.passed_validation,
    robustness: raw.robustness ? mapRobustness(raw.robustness as Record<string, unknown>) : undefined,
    featureSpec: raw.feature_spec ? mapFeatureSpec(raw.feature_spec as Record<string, unknown>) : undefined,
    validationVerdict: raw.validation_verdict
      ? mapValidationVerdict(raw.validation_verdict as Record<string, unknown>)
      : undefined,
    targetMetadata: raw.target
      ? {
          targetName: raw.target.target_name,
          horizonMinutes: raw.target.horizon_minutes,
          horizonBars: raw.target.horizon_bars,
          barMinutes: raw.target.bar_minutes,
          timezone: raw.target.timezone,
          validCount: raw.target.valid_count,
          totalCount: raw.target.total_count,
          validRatio: raw.target.valid_ratio,
          invalidReasonCounts: raw.target.invalid_reason_counts,
        }
      : null,
    dataSource: raw.data_source ? mapLakeStudyDataSource(raw.data_source) : null,
    error: raw.error ?? undefined,
  };
}

function mapQuantileBins(raw: unknown[]): QuantileBin[] {
  if (!Array.isArray(raw)) return [];
  return (raw as Record<string, unknown>[]).map(b => ({
    binNumber: b['bin_number'] as number,
    lowerBound: b['lower_bound'] as number,
    upperBound: b['upper_bound'] as number,
    meanReturn: b['mean_return'] as number,
    count: b['count'] as number,
  }));
}

function mapRobustness(r: Record<string, unknown>): Robustness {
  const monthly = (r['monthly_breakdown'] as Record<string, unknown>[] | null) ?? [];
  const rolling = (r['rolling_t_stat'] as Record<string, unknown>[] | null) ?? [];
  const volRegimes = (r['volatility_regimes'] as Record<string, unknown>[] | null) ?? [];
  const trendRegimes = (r['trend_regimes'] as Record<string, unknown>[] | null) ?? [];
  const breaks = (r['structural_breaks'] as Record<string, unknown>[] | null) ?? [];
  const tt = r['train_test'] as Record<string, unknown> | null;

  return {
    monthlyBreakdown: monthly.map((m): MonthlyICBreakdown => ({
      month: m['month'] as string,
      meanIC: m['mean_ic'] as number,
      tStat: m['t_stat'] as number,
      observationCount: m['observation_count'] as number,
    })),
    pctPositiveMonths: r['pct_positive_months'] as number,
    pctSignificantMonths: r['pct_significant_months'] as number,
    bestMonthIC: r['best_month_ic'] as number,
    worstMonthIC: r['worst_month_ic'] as number,
    stabilityLabel: r['stability_label'] as string,
    pctSignConsistentMonths: r['pct_sign_consistent_months'] as number,
    signConsistentStabilityLabel: r['sign_consistent_stability_label'] as string,
    rollingTStat: rolling.map((p): RollingTStatPoint => ({
      month: p['month'] as string,
      tStatSmoothed: p['t_stat_smoothed'] as number,
    })),
    volatilityRegimes: volRegimes.map((v): RegimeIC => ({
      regimeLabel: v['regime_label'] as string,
      meanIC: v['mean_ic'] as number,
      tStat: v['t_stat'] as number,
      observationCount: v['observation_count'] as number,
    })),
    trendRegimes: trendRegimes.map((v): RegimeIC => ({
      regimeLabel: v['regime_label'] as string,
      meanIC: v['mean_ic'] as number,
      tStat: v['t_stat'] as number,
      observationCount: v['observation_count'] as number,
    })),
    trainTest: tt
      ? {
          trainStart: tt['train_start'] as string,
          trainEnd: tt['train_end'] as string,
          testStart: tt['test_start'] as string,
          testEnd: tt['test_end'] as string,
          trainMeanIC: tt['train_mean_ic'] as number,
          trainTStat: tt['train_t_stat'] as number,
          trainDays: tt['train_days'] as number,
          testMeanIC: tt['test_mean_ic'] as number,
          testTStat: tt['test_t_stat'] as number,
          testDays: tt['test_days'] as number,
          overfitFlag: tt['overfit_flag'] as boolean,
          oosRetention: tt['oos_retention'] as number,
          oosRetentionLabel: tt['oos_retention_label'] as string,
        } satisfies TrainTestSplit
      : null,
    structuralBreaks: breaks.map((b): StructuralBreakPoint => ({
      date: b['date'] as string,
      icBefore: b['ic_before'] as number,
      icAfter: b['ic_after'] as number,
      tStat: b['t_stat'] as number,
      significant: b['significant'] as boolean,
    })),
  };
}

function mapFeatureSpec(r: Record<string, unknown>): FeatureValidationSpec {
  return {
    featureName: r['feature_name'] as string,
    defaultTarget: r['default_target'] as string,
    expectedDirection: r['expected_direction'] as string,
    expectedShape: r['expected_shape'] as string,
    stationarityRequired: r['stationarity_required'] as boolean,
    monotonicityRequired: r['monotonicity_required'] as boolean,
    isSignedTargetAppropriate: r['is_signed_target_appropriate'] as boolean,
    intent: r['intent'] as string,
    notes: (r['notes'] as string[]) ?? [],
  };
}

function mapScreen(r: Record<string, unknown>): ValidationScreen {
  return {
    name: r['name'] as string,
    description: r['description'] as string,
    passed: r['passed'] as boolean,
    requiredForStage1: r['required_for_stage1'] as boolean,
    failureReasons: (r['failure_reasons'] as string[]) ?? [],
  };
}

function mapStageInfo(si: Record<string, unknown>): FeatureStageInfo {
  const criteria = (si['advance_criteria'] as Record<string, unknown>[]) ?? [];
  return {
    stage: si['stage'] as 0 | 1 | 2 | 3,
    label: si['label'] as string,
    description: si['description'] as string,
    nextStageLabel: si['next_stage_label'] as string,
    advanceCriteria: criteria.map((c): FeatureStageCriterion => ({
      name: c['name'] as string,
      description: c['description'] as string,
      currentValue: c['current_value'] as number,
      requiredRepr: c['required_repr'] as string,
      met: c['met'] as boolean,
    })),
    failedScreens: (si['failed_screens'] as string[]) ?? [],
  };
}

function mapValidationVerdict(r: Record<string, unknown>): FeatureValidationVerdict {
  const mt = r['multiple_testing'] as Record<string, unknown>;
  const cv = r['cost_viability'] as Record<string, unknown>;
  const ci = r['ic_ci'] as Record<string, unknown>;
  const si = r['stage_info'] as Record<string, unknown> | null | undefined;

  return {
    statisticalScreen: mapScreen(r['statistical_screen'] as Record<string, unknown>),
    economicScreen: mapScreen(r['economic_screen'] as Record<string, unknown>),
    oosScreen: mapScreen(r['oos_screen'] as Record<string, unknown>),
    multipleTestingScreen: mapScreen(r['multiple_testing_screen'] as Record<string, unknown>),
    regimeStabilityScreen: mapScreen(r['regime_stability_screen'] as Record<string, unknown>),
    multipleTesting: {
      rawNwPValue: mt['raw_nw_p_value'] as number,
      holmPValue: mt['holm_p_value'] as number,
      nFamily: mt['n_family'] as number,
      note: mt['note'] as string,
    } satisfies MultipleTestingWarning,
    costViability: {
      grossSpreadBpsSigned: cv['gross_spread_bps_signed'] as number,
      directionalSpreadBps: cv['directional_spread_bps'] as number,
      costAssumptionOneWayBps: cv['cost_assumption_one_way_bps'] as number,
      costErasureOneWayBps: cv['cost_erasure_one_way_bps'] as number,
      netSpreadBpsAtAssumption: cv['net_spread_bps_at_assumption'] as number,
      viableAtAssumption: cv['viable_at_assumption'] as boolean,
      specDirection: cv['spec_direction'] as string,
      note: cv['note'] as string,
    } satisfies CostViability,
    icCi: {
      point: ci['point'] as number,
      se: ci['se'] as number,
      ciLower: ci['ci_lower'] as number,
      ciUpper: ci['ci_upper'] as number,
      confidenceLevel: ci['confidence_level'] as number,
      nEffUsed: ci['n_eff_used'] as number,
      valid: ci['valid'] as boolean,
      seApproximationNote: ci['se_approximation_note'] as string,
    } satisfies IcCi,
    directionMatchesSpec: r['direction_matches_spec'] as boolean,
    targetSignedAppropriate: r['target_signed_appropriate'] as boolean,
    stageInfo: si ? mapStageInfo(si) : undefined,
    finalDecision: r['final_decision'] as string,
  };
}
