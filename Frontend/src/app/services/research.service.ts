import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, map, tap } from 'rxjs';
import { environment } from '../../environments/environment';

const GRAPHQL_URL = environment.backendUrl;

// ─── Interfaces ────────────────────────────────────────────

export interface QuantileBin {
  binNumber: number;
  lowerBound: number;
  upperBound: number;
  meanReturn: number;
  count: number;
}

export interface MonthlyICBreakdown {
  month: string;
  meanIC: number;
  tStat: number;
  observationCount: number;
}

export interface RollingTStatPoint {
  month: string;
  tStatSmoothed: number;
}

export interface RegimeIC {
  regimeLabel: string;
  meanIC: number;
  tStat: number;
  observationCount: number;
}

export interface TrainTestSplit {
  trainStart: string;
  trainEnd: string;
  testStart: string;
  testEnd: string;
  trainMeanIC: number;
  trainTStat: number;
  trainDays: number;
  testMeanIC: number;
  testTStat: number;
  testDays: number;
  overfitFlag: boolean;
  oosRetention: number;
  oosRetentionLabel: string;
}

export interface StructuralBreakPoint {
  date: string;
  icBefore: number;
  icAfter: number;
  tStat: number;
  significant: boolean;
}

export interface Robustness {
  monthlyBreakdown: MonthlyICBreakdown[];
  pctPositiveMonths: number;
  pctSignificantMonths: number;
  bestMonthIC: number;
  worstMonthIC: number;
  stabilityLabel: string;
  pctSignConsistentMonths: number;
  signConsistentStabilityLabel: string;
  rollingTStat: RollingTStatPoint[];
  volatilityRegimes: RegimeIC[];
  trendRegimes: RegimeIC[];
  trainTest: TrainTestSplit | null;
  structuralBreaks: StructuralBreakPoint[];
}

export interface FeatureValidationSpec {
  featureName: string;
  defaultTarget: string;
  /** "positive" | "negative" | "two_sided" | "unknown" */
  expectedDirection: string;
  /** "monotonic_increasing" | "monotonic_decreasing" | "u_shaped" | "inverted_u" | "tail_only" | "none" */
  expectedShape: string;
  stationarityRequired: boolean;
  monotonicityRequired: boolean;
  /** False when signed forward return is the wrong question for this
   *  feature (e.g. realized_vol_30). Triggers Stage 0 with a "wrong
   *  target" reason instead of letting a near-zero IC look like a
   *  feature failure. */
  isSignedTargetAppropriate: boolean;
  intent: string;
  notes: string[];
}

export interface InvalidReasonCount {
  /** "cross_session" | "window_gap" | "non_positive_close" | "window_runs_off_end" */
  reason: string;
  count: number;
}

export interface TargetMetadata {
  targetName: string;
  horizonMinutes: number;
  horizonBars: number;
  barMinutes: number;
  /** Trading-session timezone used for cross-day masking. */
  timezone: string;
  validCount: number;
  totalCount: number;
  validRatio: number;
  /** Drop-reason breakdown. Hot Chocolate exposes the underlying
   *  Dictionary<string, int> as a list of objects, so this is a list
   *  rather than a map. */
  invalidReasonCounts: InvalidReasonCount[];
}

export interface IcCi {
  point: number;
  se: number;
  ciLower: number;
  ciUpper: number;
  confidenceLevel: number;
  nEffUsed: number;
  valid: boolean;
  seApproximationNote: string;
}

export interface MultipleTestingWarning {
  rawNwPValue: number;
  holmPValue: number;
  nFamily: number;
  note: string;
}

export interface CostViability {
  /** Raw Q5 - Q1 spread in bps, sign preserved. Negative when the top
   *  quintile underperforms the bottom quintile. */
  grossSpreadBpsSigned: number;
  /** Spec-direction-aligned spread:
   *  positive direction = Q5-Q1, negative = Q1-Q5,
   *  two_sided/unknown = |Q5-Q1|. The economic gate uses this. */
  directionalSpreadBps: number;
  costAssumptionOneWayBps: number;
  costErasureOneWayBps: number;
  netSpreadBpsAtAssumption: number;
  viableAtAssumption: boolean;
  /** Direction the cost calc was anchored on. */
  specDirection: string;
  note: string;
}

export interface ValidationScreen {
  name: string;
  description: string;
  passed: boolean;
  requiredForStage1: boolean;
  failureReasons: string[];
}

export interface FeatureStageCriterion {
  name: string;
  description: string;
  currentValue: number;
  requiredRepr: string;
  met: boolean;
}

export interface FeatureStageInfo {
  /** 0 = Rejected, 1 = Statistical association, 2 = Research candidate, 3 = Paper-trading candidate. */
  stage: 0 | 1 | 2 | 3;
  label: string;
  description: string;
  nextStageLabel: string;
  advanceCriteria: FeatureStageCriterion[];
  failedScreens: string[];
}

export interface FeatureValidationVerdict {
  statisticalScreen: ValidationScreen;
  economicScreen: ValidationScreen;
  oosScreen: ValidationScreen;
  multipleTestingScreen: ValidationScreen;
  /** Promoted from a hidden Stage 2 sub-criterion to a first-class
   *  screen — gates Stage 2+. */
  regimeStabilityScreen: ValidationScreen;
  multipleTesting: MultipleTestingWarning;
  costViability: CostViability;
  icCi: IcCi;
  /** False when the headline IC sign disagrees with the spec's
   *  `expectedDirection`. The statistical screen reports this as
   *  "inverse signal discovered" rather than a clean pass. */
  directionMatchesSpec: boolean;
  /** False when the spec marks signed forward return as the wrong
   *  target (e.g. realized_vol_30). Stage 0 fires immediately. */
  targetSignedAppropriate: boolean;
  stageInfo: FeatureStageInfo;
  finalDecision: string;
}

export interface ResearchResult {
  success: boolean;
  ticker: string;
  featureName: string;
  startDate: string;
  endDate: string;
  barsUsed: number;
  meanIC: number;
  icTStat: number;
  icPValue: number;
  nwTStat: number;
  nwPValue: number;
  effectiveN: number;
  icValues: number[];
  icDates: string[];
  adfPvalue: number;
  kpssPvalue: number;
  isStationary: boolean;
  quantileBins: QuantileBin[];
  isMonotonic: boolean;
  monotonicityRatio: number;
  passedValidation: boolean;
  robustness?: Robustness;
  featureSpec?: FeatureValidationSpec | null;
  /** Audit trail of what the target pipeline actually computed. */
  targetMetadata?: TargetMetadata | null;
  validationVerdict?: FeatureValidationVerdict | null;
  error?: string;
}

export interface ResearchExperiment {
  id: number;
  ticker: string;
  featureName: string;
  startDate: string;
  endDate: string;
  barsUsed: number;
  meanIC: number;
  icTStat: number;
  icPValue: number;
  adfPValue: number;
  kpssPValue: number;
  isStationary: boolean;
  passedValidation: boolean;
  monotonicityRatio: number;
  isMonotonic: boolean;
  createdAt: number;
}

// ─── Signal Engine Interfaces ─────────────────────────────────

export interface SignalBacktestResult {
  threshold: number;
  costBps: number;
  dates: string[];
  cumulativeReturns: number[];
  positions: number[];
  grossSharpe: number;
  netSharpe: number;
  maxDrawdown: number;
  annualizedTurnover: number;
  avgHoldingBars: number;
  winRate: number;
  avgWinLossRatio: number;
  totalTrades: number;
  netTotalReturn: number;
  grossTotalReturn: number;
}

export interface WalkForwardWindow {
  foldIndex: number;
  trainStart: string;
  trainEnd: string;
  testStart: string;
  testEnd: string;
  trainBars: number;
  testBars: number;
  mu: number;
  sigma: number;
  bestThreshold: number;
  oosNetSharpe: number;
  oosGrossSharpe: number;
  oosMaxDrawdown: number;
  oosNetReturn: number;
  oosWinRate: number;
  oosTotalTrades: number;
  oosDates: string[];
  oosCumulativeReturns: number[];
}

export interface WalkForwardResult {
  windows: WalkForwardWindow[];
  meanOosSharpe: number;
  stdOosSharpe: number;
  medianOosSharpe: number;
  pctWindowsProfitable: number;
  pctWindowsPositiveSharpe: number;
  worstWindowSharpe: number;
  bestWindowSharpe: number;
  totalOosBars: number;
  combinedOosDates: string[];
  combinedOosCumulativeReturns: number[];
  oosSharpeTrendSlope: number;
  alphaDecay: AlphaDecayStats | null;
}

export interface GraduationCriterion {
  name: string;
  description: string;
  passed: boolean;
  value: number;
  threshold: number;
  label: string;
  failureReason: string;
}

export interface ThresholdSharpeEntry {
  threshold: number;
  sharpe: number;
}

export interface ParameterStability {
  sharpeValuesByThreshold: ThresholdSharpeEntry[];
  stabilityScore: number;
  stabilityLabel: string;
}

export interface GraduationResult {
  criteria: GraduationCriterion[];
  overallPassed: boolean;
  overallGrade: string;
  summary: string;
  statusLabel: string;
  parameterStability: ParameterStability | null;
  stage0Rejection: Stage0Rejection | null;
  stageInfo: GraduationStageInfo | null;
}

export interface SignalDiagnostics {
  signalMean: number;
  signalStd: number;
  pctTimeActive: number;
  avgAbsSignal: number;
  pctFilteredByThreshold: number;
  pctGatedByRegime: number;
}

export interface RegimeCoverageEntry {
  regime: string;
  count: number;
}

export interface DataSufficiency {
  totalBars: number;
  trainBars: number;
  testBars: number;
  walkForwardFolds: number;
  effectiveOosBars: number;
  regimesCovered: number;
  regimeCoverage: RegimeCoverageEntry[];
  coverageWarnings: string[];
}

export interface EffectiveSampleSize {
  rawN: number;
  effectiveN: number;
  autocorrelationLag1: number;
  independentBets: number;
  maxLagUsed: number;
  rhoSum: number;
}

export interface AlphaDecayStats {
  slope: number;
  intercept: number;
  tStat: number;
  pValue: number;
  rSquared: number;
  nFoldsUsed: number;
  /** False when fewer than 5 folds — UI must render "insufficient folds"
   *  instead of the (uninformative) p-value in that case. */
  isTestValid: boolean;
  /** True only when ``isTestValid`` AND p < 0.05. */
  isSignificant: boolean;
}

export interface SharpeCi {
  point: number;
  se: number;
  ciLower: number;
  ciUpper: number;
  confidenceLevel: number;
  nEffUsed: number;
  valid: boolean;
}

export interface DeflatedSharpe {
  rawSharpe: number;
  expectedMaxUnderNull: number;
  dsrProbability: number;
  nTrials: number;
  skewness: number;
  kurtosis: number;
  valid: boolean;
}

export interface RegimeBucket {
  volLabel: string;
  trendLabel: string;
  days: number;
  effectiveTrades: number;
  badge: 'Pass' | 'Sparse' | 'Empty' | string;
}

export interface Stage0Failure {
  criterionName: string;
  value: number;
  thresholdRepr: string;
  message: string;
}

export interface Stage0Rejection {
  rejected: boolean;
  failedCriteria: Stage0Failure[];
}

export interface StageAdvanceCriterion {
  name: string;
  description: string;
  currentValue: number;
  requiredRepr: string;
  met: boolean;
}

export interface GraduationStageInfo {
  /** 0 = Rejected, 1 = Weak Candidate, 2 = Research Candidate, 3 = Promotion Candidate. */
  stage: 0 | 1 | 2 | 3;
  label: string;
  description: string;
  /** Empty when at the top of the ladder. */
  nextStageLabel: string;
  advanceCriteria: StageAdvanceCriterion[];
}

export interface SignalBehaviorMetrics {
  avgForwardReturnWhenActive: number;
  skewnessActiveReturns: number;
  avgWinReturn: number;
  avgLossReturn: number;
  hitRate: number;
}

export interface Methodology {
  trainMonths: number;
  testMonths: number;
  windowType: string;
  optimizationTarget: string;
  annualizationFactor: number;
  barsPerDay: number;
  horizon: number;
  defaultCostBps: number;
  minBarsForSignal: number;
  flipSign: boolean;
  regimeGateEnabled: boolean;
  /** Backend may omit these on older history records persisted before
   *  the field was added; templates must coalesce to `[]`. */
  thresholds?: number[] | null;
  costBpsOptions?: number[] | null;
}

export interface SignalEngineResult {
  success: boolean;
  ticker: string;
  featureName: string;
  startDate: string;
  endDate: string;
  barsUsed: number;
  flipSign: boolean;
  thresholdsTested: number[];
  costBpsOptions: number[];
  bestThreshold: number;
  bestCostBps: number;
  backtestGrid: SignalBacktestResult[];
  walkForward: WalkForwardResult | null;
  graduation: GraduationResult | null;
  signalDiagnostics: SignalDiagnostics | null;
  dataSufficiency: DataSufficiency | null;
  effectiveSample: EffectiveSampleSize | null;
  regimeCoverage: RegimeCoverageEntry[];
  /** Joint (vol × trend) regime distribution with effective-trades estimates.
   *  Replaces the marginal `regimeCoverage` for new consumers. */
  jointRegimeCoverage: RegimeBucket[];
  signalBehavior: SignalBehaviorMetrics | null;
  /** Lo (2002) confidence interval for the headline OOS Sharpe. */
  oosSharpeCi: SharpeCi | null;
  /** Bailey & López de Prado DSR for the IS grid headline. */
  deflatedSharpe: DeflatedSharpe | null;
  methodology: Methodology | null;
  researchLog: string;
  error?: string;
}

export interface SignalExperiment {
  id: number;
  ticker: string;
  featureName: string;
  startDate: string;
  endDate: string;
  barsUsed: number;
  overallGrade: string;
  statusLabel: string;
  overallPassed: boolean;
  meanOosSharpe: number;
  bestThreshold: number;
  bestCostBps: number;
  flipSign: boolean;
  regimeGateEnabled: boolean;
  createdAt: number;
}

// ─── Batch / Cross-Sectional Interfaces ──────────────────

export type TickerValidity = 'valid' | 'invalid_iv' | 'invalid_data' | 'error';

export interface TickerBatchResult {
  ticker: string;
  meanIc: number;
  icTStat: number;
  icPValue: number;
  nwTStat: number;
  nwPValue: number;
  effectiveN: number;
  isStationary: boolean;
  passedValidation: boolean;
  dataPoints: number;
  error?: string;
  /** Distinguishes "FAIL — IC ran, signal weak" from "INVALID — IV
   *  diagnostics failed, no IC computation". */
  validity?: TickerValidity;
  /** True when the ticker is technically valid (IC ran) but its
   *  effective sample size is below the trust threshold (default 10).
   *  Surfaced in the UI as a "low confidence" annotation on the row. */
  lowConfidence?: boolean;
}

export interface ValiditySummary {
  valid: number;
  invalidIv: number;
  invalidData: number;
  errored: number;
}

export interface AggregateIcCi {
  point: number;
  se: number;
  ciLower: number;
  ciUpper: number;
  confidenceLevel: number;
  weightingMethod: string;
  /** Disclaimer about the SE approximation used (Stage 1 / Stage 2 form
   *  vs. full Lo (2002) form). Rendered in the CI tooltip. */
  seApproximationNote?: string;
  nTickersUsed: number;
  sumWeights: number;
  valid: boolean;
}

export interface BinomialNullTest {
  nValid: number;
  nEffAssets: number;
  nPassed: number;
  alphaPerTicker: number;
  pValue: number;
  significant: boolean;
}

export interface CrossSectionalCriterion {
  name: string;
  description: string;
  currentValue: number;
  requiredRepr: string;
  met: boolean;
}

export interface CrossSectionalStageInfo {
  /** 0 = Rejected, 1 = Weak, 2 = Research candidate, 3 = Promotion. */
  stage: 0 | 1 | 2 | 3;
  label: string;
  description: string;
  nextStageLabel: string;
  failedCriteria: CrossSectionalCriterion[];
  advanceCriteria: CrossSectionalCriterion[];
}

export interface BatchResearchResult {
  success: boolean;
  featureName: string;
  tickersTested: number;
  tickersPassed: number;
  passRate: number;
  crossSectionalConsistent: boolean;
  aggregateIc: number;
  tickerResults: TickerBatchResult[];
  summary: string;
  // Optional — populated by the SSE-driven job path.
  tickersTestedRaw?: number;
  tickersValid?: number;
  validitySummary?: ValiditySummary;
  aggregateIcUniform?: number;
  aggregateIcCi?: AggregateIcCi;
  binomialTest?: BinomialNullTest;
  nEffAssets?: number;
  /** Which input matrix drove `nEffAssets` — `'ic'` (Stage-2-correct,
   *  per-ticker IC time series) or `'returns'` (Stage-1 fallback,
   *  daily stock returns). Disclosed via tooltip. */
  nEffAssetsMethod?: 'ic' | 'returns';
  stageInfo?: CrossSectionalStageInfo;
  error?: string;
}

// ─── GraphQL Queries ───────────────────────────────────────

const GET_RESEARCH_EXPERIMENTS_QUERY = `
  query GetResearchExperiments($ticker: String!) {
    getResearchExperiments(ticker: $ticker) {
      id ticker featureName startDate endDate barsUsed
      meanIC icTStat icPValue adfPValue kpssPValue
      isStationary passedValidation
      monotonicityRatio isMonotonic createdAt
    }
  }
`;

const GET_SIGNAL_EXPERIMENTS_QUERY = `
  query GetSignalExperiments($ticker: String!) {
    getSignalExperiments(ticker: $ticker) {
      id ticker featureName startDate endDate barsUsed
      overallGrade statusLabel overallPassed
      meanOosSharpe bestThreshold bestCostBps
      flipSign regimeGateEnabled createdAt
    }
  }
`;

const GET_SIGNAL_EXPERIMENT_REPORT_QUERY = `
  query GetSignalExperimentReport($id: Int!) {
    getSignalExperimentReport(id: $id) {
      success ticker featureName startDate endDate barsUsed
      flipSign thresholdsTested costBpsOptions bestThreshold bestCostBps
      backtestGrid {
        threshold costBps dates cumulativeReturns positions
        grossSharpe netSharpe maxDrawdown annualizedTurnover
        avgHoldingBars winRate avgWinLossRatio totalTrades
        netTotalReturn grossTotalReturn
      }
      walkForward {
        windows {
          foldIndex trainStart trainEnd testStart testEnd
          trainBars testBars mu sigma bestThreshold
          oosNetSharpe oosGrossSharpe oosMaxDrawdown
          oosNetReturn oosWinRate oosTotalTrades
          oosDates oosCumulativeReturns
        }
        meanOosSharpe stdOosSharpe medianOosSharpe
        pctWindowsProfitable pctWindowsPositiveSharpe
        worstWindowSharpe bestWindowSharpe totalOosBars
        combinedOosDates combinedOosCumulativeReturns
        oosSharpeTrendSlope
        alphaDecay {
          slope intercept tStat pValue rSquared
          nFoldsUsed isTestValid isSignificant
        }
      }
      graduation {
        criteria {
          name description passed value threshold label failureReason
        }
        overallPassed overallGrade summary statusLabel
        parameterStability {
          sharpeValuesByThreshold { threshold sharpe }
          stabilityScore stabilityLabel
        }
        stage0Rejection {
          rejected
          failedCriteria { criterionName value thresholdRepr message }
        }
        stageInfo {
          stage label description nextStageLabel
          advanceCriteria { name description currentValue requiredRepr met }
        }
      }
      signalDiagnostics {
        signalMean signalStd pctTimeActive avgAbsSignal
        pctFilteredByThreshold pctGatedByRegime
      }
      dataSufficiency {
        totalBars trainBars testBars walkForwardFolds
        effectiveOosBars regimesCovered
        regimeCoverage { regime count }
        coverageWarnings
      }
      effectiveSample {
        rawN effectiveN autocorrelationLag1 independentBets
        maxLagUsed rhoSum
      }
      regimeCoverage { regime count }
      jointRegimeCoverage {
        volLabel trendLabel days effectiveTrades badge
      }
      signalBehavior {
        avgForwardReturnWhenActive skewnessActiveReturns
        avgWinReturn avgLossReturn hitRate
      }
      oosSharpeCi {
        point se ciLower ciUpper confidenceLevel nEffUsed valid
      }
      deflatedSharpe {
        rawSharpe expectedMaxUnderNull dsrProbability
        nTrials skewness kurtosis valid
      }
      methodology {
        trainMonths testMonths windowType optimizationTarget
        annualizationFactor barsPerDay horizon defaultCostBps
        minBarsForSignal flipSign regimeGateEnabled
        thresholds costBpsOptions
      }
      researchLog error
    }
  }
`;

// ─── Response Types ────────────────────────────────────────

interface GetExperimentsResponse {
  data: { getResearchExperiments: ResearchExperiment[] };
  errors?: { message: string }[];
}

interface GetSignalExperimentsResponse {
  data: { getSignalExperiments: SignalExperiment[] };
  errors?: { message: string }[];
}

interface GetSignalExperimentReportResponse {
  data: { getSignalExperimentReport: SignalEngineResult | null };
  errors?: { message: string }[];
}

// ─── Service ───────────────────────────────────────────────

@Injectable({
  providedIn: 'root'
})
export class ResearchService {
  private http = inject(HttpClient);

  getExperiments(ticker: string): Observable<ResearchExperiment[]> {
    return this.http
      .post<GetExperimentsResponse>(GRAPHQL_URL, {
        query: GET_RESEARCH_EXPERIMENTS_QUERY,
        variables: { ticker }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getResearchExperiments)
      );
  }

  getSignalExperiments(ticker: string): Observable<SignalExperiment[]> {
    return this.http
      .post<GetSignalExperimentsResponse>(GRAPHQL_URL, {
        query: GET_SIGNAL_EXPERIMENTS_QUERY,
        variables: { ticker }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getSignalExperiments)
      );
  }

  getSignalExperimentReport(id: number): Observable<SignalEngineResult | null> {
    return this.http
      .post<GetSignalExperimentReportResponse>(GRAPHQL_URL, {
        query: GET_SIGNAL_EXPERIMENT_REPORT_QUERY,
        variables: { id }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getSignalExperimentReport)
      );
  }
}
