import {
  BatchResearchResult,
  CrossSectionalCriterion,
  TickerValidity,
} from '../../../services/research.service';

/** Snake-case shape returned by the cross-sectional jobs-internal worker. */
export interface CrossSectionalJobResultRaw {
  success: boolean;
  feature_name: string;
  target_type?: string;
  tickers_tested: number;
  tickers_tested_raw?: number;
  tickers_valid?: number;
  tickers_passed: number;
  pass_rate: number;
  cross_sectional_consistent: boolean;
  aggregate_ic: number;
  aggregate_ic_uniform?: number;
  aggregate_ic_ci?: {
    point: number;
    se: number;
    ci_lower: number;
    ci_upper: number;
    confidence_level: number;
    weighting_method: string;
    se_approximation_note?: string;
    n_tickers_used: number;
    sum_weights: number;
    valid: boolean;
  };
  binomial_test?: {
    n_valid: number;
    n_eff_assets: number;
    n_passed: number;
    alpha_per_ticker: number;
    p_value: number;
    significant: boolean;
  };
  n_eff_assets?: number;
  n_eff_assets_method?: 'ic' | 'returns';
  validity_summary?: {
    valid: number;
    invalid_iv: number;
    invalid_data: number;
    errored: number;
  };
  stage_info?: {
    stage: number;
    label: string;
    description: string;
    next_stage_label: string;
    failed_criteria: {
      name: string;
      description: string;
      current_value: number;
      required_repr: string;
      met: boolean;
    }[];
    advance_criteria: {
      name: string;
      description: string;
      current_value: number;
      required_repr: string;
      met: boolean;
    }[];
  };
  ticker_results: {
    ticker: string;
    mean_ic: number;
    ic_t_stat: number;
    ic_p_value: number;
    nw_t_stat: number;
    nw_p_value: number;
    effective_n: number;
    is_stationary: boolean;
    passed_validation: boolean;
    data_points: number;
    error: string | null;
    validity?: TickerValidity;
    low_confidence?: boolean;
  }[];
  summary: string;
}

/** Transform the snake_case worker result into the camelCase
 *  ``BatchResearchResult`` interface used by the table template. */
export function toBatchResearchResult(raw: CrossSectionalJobResultRaw): BatchResearchResult {
  return {
    success: raw.success,
    featureName: raw.feature_name,
    tickersTested: raw.tickers_tested,
    tickersPassed: raw.tickers_passed,
    passRate: raw.pass_rate,
    crossSectionalConsistent: raw.cross_sectional_consistent,
    aggregateIc: raw.aggregate_ic,
    tickerResults: raw.ticker_results.map((r) => ({
      ticker: r.ticker,
      meanIc: r.mean_ic,
      icTStat: r.ic_t_stat,
      icPValue: r.ic_p_value,
      nwTStat: r.nw_t_stat,
      nwPValue: r.nw_p_value,
      effectiveN: r.effective_n,
      isStationary: r.is_stationary,
      passedValidation: r.passed_validation,
      dataPoints: r.data_points,
      error: r.error ?? undefined,
      validity: r.validity,
      lowConfidence: r.low_confidence,
    })),
    summary: raw.summary,
    tickersTestedRaw: raw.tickers_tested_raw,
    tickersValid: raw.tickers_valid,
    validitySummary: raw.validity_summary
      ? {
          valid: raw.validity_summary.valid,
          invalidIv: raw.validity_summary.invalid_iv,
          invalidData: raw.validity_summary.invalid_data,
          errored: raw.validity_summary.errored,
        }
      : undefined,
    aggregateIcUniform: raw.aggregate_ic_uniform,
    aggregateIcCi: raw.aggregate_ic_ci
      ? {
          point: raw.aggregate_ic_ci.point,
          se: raw.aggregate_ic_ci.se,
          ciLower: raw.aggregate_ic_ci.ci_lower,
          ciUpper: raw.aggregate_ic_ci.ci_upper,
          confidenceLevel: raw.aggregate_ic_ci.confidence_level,
          weightingMethod: raw.aggregate_ic_ci.weighting_method,
          seApproximationNote: raw.aggregate_ic_ci.se_approximation_note,
          nTickersUsed: raw.aggregate_ic_ci.n_tickers_used,
          sumWeights: raw.aggregate_ic_ci.sum_weights,
          valid: raw.aggregate_ic_ci.valid,
        }
      : undefined,
    binomialTest: raw.binomial_test
      ? {
          nValid: raw.binomial_test.n_valid,
          nEffAssets: raw.binomial_test.n_eff_assets,
          nPassed: raw.binomial_test.n_passed,
          alphaPerTicker: raw.binomial_test.alpha_per_ticker,
          pValue: raw.binomial_test.p_value,
          significant: raw.binomial_test.significant,
        }
      : undefined,
    nEffAssets: raw.n_eff_assets,
    nEffAssetsMethod: raw.n_eff_assets_method,
    stageInfo: raw.stage_info
      ? {
          stage: raw.stage_info.stage as 0 | 1 | 2 | 3,
          label: raw.stage_info.label,
          description: raw.stage_info.description,
          nextStageLabel: raw.stage_info.next_stage_label,
          failedCriteria: raw.stage_info.failed_criteria.map(toCriterion),
          advanceCriteria: raw.stage_info.advance_criteria.map(toCriterion),
        }
      : undefined,
  };
}

function toCriterion(c: {
  name: string;
  description: string;
  current_value: number;
  required_repr: string;
  met: boolean;
}): CrossSectionalCriterion {
  return {
    name: c.name,
    description: c.description,
    currentValue: c.current_value,
    requiredRepr: c.required_repr,
    met: c.met,
  };
}
