import {
  BatchResearchResult,
  CrossSectionalCriterion,
  CrossSectionalDataSource,
  CrossSectionalFailure,
  LakeOptionInputsSource,
  MemberExclusion,
  OptionInputCounts,
  OptionInputIssue,
  OptionInputsMember,
  OptionInputsSource,
  RiskFreeRateSource,
  StudyUniverse,
  TickerValidity,
} from '../../../services/research.service';
import {
  mapLakeStudyDataSource,
  readLakeStudyRefusal,
  type LakeDataSourceRaw,
} from '../../../shared/lake-study/lake-study';

interface OptionInputCountsRaw {
  expected: number;
  reused: number;
  added: number;
}

interface OptionInputIssueRaw {
  kind: 'listing' | 'contract_day';
  trading_date_ms: number;
  subject: string;
  outcome: string;
  reason: string;
  failure_reason: string | null;
  retryable: boolean;
}

interface OptionInputsMemberRaw {
  ticker: string;
  status: 'admitted' | 'excluded';
  digest?: string;
  source?: OptionInputsSource;
  listings?: OptionInputCountsRaw;
  contract_days?: OptionInputCountsRaw & { from_archive: number };
  code?: string;
  reason?: string;
  retryable?: boolean;
  issues?: OptionInputIssueRaw[];
  issues_total?: number;
}

/** The ``data_source`` block (``cross_sectional_study_service.data_source_block``). */
export interface CrossSectionalDataSourceRaw {
  kind: 'lake';
  calculation_version: string;
  /** The block every lake study writes; a universe's counts are member-sessions. */
  stock_bars: LakeDataSourceRaw | null;
  /** The listings and contract bars the IV history is built from (``option_inputs.option_inputs_block``). */
  option_inputs: {
    kind: 'lake';
    policy: string;
    price_rule_words: string;
    digest: string | null;
    listings: OptionInputCountsRaw;
    contract_days: OptionInputCountsRaw & { from_archive: number };
    members: OptionInputsMemberRaw[];
  } | null;
  /** FRED, asked live: the one input the lake does not hold. */
  risk_free_rate: {
    kind: 'provider';
    provider: string;
    days_from_fred: number;
    days_from_default: number;
    days_fetch_failed: number;
    values_digest: string | null;
  } | null;
}

export interface StudyUniverseRaw {
  requested: string[];
  requested_count: number;
  evaluated: string[];
  evaluated_count: number;
  excluded: MemberExclusion[];
  excluded_count: number;
  degraded: { ticker: string; rate_days_fetch_failed: number }[];
  denominator: number;
  inclusion_policy: { name: string; description: string };
  complete: boolean;
}

/** What a study that ended without a result parks as the job's result. */
export interface CrossSectionalFailedResultRaw {
  success: false;
  status: 'refused' | 'not_evaluated';
  universe?: StudyUniverseRaw;
  data_source?: CrossSectionalDataSourceRaw;
}

/** Snake-case shape returned by the cross-sectional jobs-internal worker. */
export interface CrossSectionalJobResultRaw {
  success: boolean;
  status?: 'complete' | 'partial';
  universe?: StudyUniverseRaw;
  data_source?: CrossSectionalDataSourceRaw;
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
    exclusion?: MemberExclusion | null;
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
      exclusion: r.exclusion ?? undefined,
    })),
    summary: raw.summary,
    status: raw.status,
    universe: raw.universe ? toUniverse(raw.universe) : undefined,
    dataSource: raw.data_source ? toDataSource(raw.data_source) : undefined,
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

function toUniverse(u: StudyUniverseRaw): StudyUniverse {
  return {
    requested: u.requested,
    requestedCount: u.requested_count,
    evaluated: u.evaluated,
    evaluatedCount: u.evaluated_count,
    excluded: u.excluded,
    excludedCount: u.excluded_count,
    degraded: u.degraded.map((d) => ({ ticker: d.ticker, rateDaysFetchFailed: d.rate_days_fetch_failed })),
    denominator: u.denominator,
    inclusionPolicy: u.inclusion_policy,
    complete: u.complete,
  };
}

function toCounts(c: OptionInputCountsRaw): OptionInputCounts {
  return { expected: c.expected, reused: c.reused, added: c.added };
}

function toIssue(i: OptionInputIssueRaw): OptionInputIssue {
  return {
    kind: i.kind,
    tradingDateMs: i.trading_date_ms,
    subject: i.subject,
    outcome: i.outcome,
    reason: i.reason,
    failureReason: i.failure_reason,
    retryable: i.retryable,
  };
}

function toOptionMember(m: OptionInputsMemberRaw): OptionInputsMember {
  return {
    ticker: m.ticker,
    status: m.status,
    digest: m.digest,
    source: m.source,
    listings: m.listings ? toCounts(m.listings) : undefined,
    contractDays: m.contract_days ? { ...toCounts(m.contract_days), fromArchive: m.contract_days.from_archive } : undefined,
    code: m.code,
    reason: m.reason,
    retryable: m.retryable,
    issues: m.issues?.map(toIssue),
    issuesTotal: m.issues_total,
  };
}

function toDataSource(raw: CrossSectionalDataSourceRaw): CrossSectionalDataSource {
  const options = raw.option_inputs;
  const rate = raw.risk_free_rate;
  return {
    kind: raw.kind,
    calculationVersion: raw.calculation_version,
    stockBars: raw.stock_bars ? mapLakeStudyDataSource(raw.stock_bars) : null,
    optionInputs: options ? toOptionInputs(options) : null,
    riskFreeRate: rate ? toRiskFreeRate(rate) : null,
  };
}

function toOptionInputs(options: NonNullable<CrossSectionalDataSourceRaw['option_inputs']>): LakeOptionInputsSource {
  return {
    kind: options.kind,
    policy: options.policy,
    priceRuleWords: options.price_rule_words,
    digest: options.digest,
    listings: toCounts(options.listings),
    contractDays: { ...toCounts(options.contract_days), fromArchive: options.contract_days.from_archive },
    members: options.members.map(toOptionMember),
  };
}

function toRiskFreeRate(rate: NonNullable<CrossSectionalDataSourceRaw['risk_free_rate']>): RiskFreeRateSource {
  return {
    kind: rate.kind,
    provider: rate.provider,
    daysFromFred: rate.days_from_fred,
    daysFromDefault: rate.days_from_default,
    daysFetchFailed: rate.days_fetch_failed,
    valuesDigest: rate.values_digest,
  };
}

/**
 * Why a run that ended without a result has none, from the payload the failed job parked: the lake's refusal (the
 * source it is read against is the stock-bars part of the block), the universe, and the source.
 */
export function toCrossSectionalFailure(raw: CrossSectionalFailedResultRaw): CrossSectionalFailure {
  return {
    refused: readLakeStudyRefusal({ ...raw, data_source: raw.data_source?.stock_bars ?? null }),
    universe: raw.universe ? toUniverse(raw.universe) : null,
    dataSource: raw.data_source ? toDataSource(raw.data_source) : null,
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
