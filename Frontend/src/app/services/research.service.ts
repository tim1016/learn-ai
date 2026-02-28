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
  createdAt: string;
}

export interface FeatureInfo {
  name: string;
  display_name: string;
  formula_latex: string;
  variables: string;
  example: string;
  interpretation: string;
  implementation_note: string;
  window: number;
  category: string;
}

export interface RunFeatureResearchInput {
  ticker: string;
  featureName: string;
  fromDate: string;
  toDate: string;
  timespan?: string;
  multiplier?: number;
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
  thresholds: number[];
  costBpsOptions: number[];
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
  signalBehavior: SignalBehaviorMetrics | null;
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
  createdAt: string;
}

export interface RunSignalEngineInput {
  ticker: string;
  featureName: string;
  fromDate: string;
  toDate: string;
  flipSign: boolean;
  regimeGateEnabled: boolean;
  timespan?: string;
  multiplier?: number;
  forceRefresh?: boolean;
}

// ─── GraphQL Queries ───────────────────────────────────────

const RUN_FEATURE_RESEARCH_MUTATION = `
  mutation RunFeatureResearch(
    $ticker: String!
    $featureName: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "minute"
    $multiplier: Int! = 1
  ) {
    runFeatureResearch(
      ticker: $ticker
      featureName: $featureName
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
    ) {
      success ticker featureName startDate endDate barsUsed
      meanIC icTStat icPValue nwTStat nwPValue effectiveN
      icValues icDates
      adfPvalue kpssPvalue isStationary
      quantileBins { binNumber lowerBound upperBound meanReturn count }
      isMonotonic monotonicityRatio
      passedValidation
      robustness {
        monthlyBreakdown { month meanIC tStat observationCount }
        pctPositiveMonths pctSignificantMonths
        bestMonthIC worstMonthIC stabilityLabel
        pctSignConsistentMonths signConsistentStabilityLabel
        rollingTStat { month tStatSmoothed }
        volatilityRegimes { regimeLabel meanIC tStat observationCount }
        trendRegimes { regimeLabel meanIC tStat observationCount }
        trainTest {
          trainStart trainEnd testStart testEnd
          trainMeanIC trainTStat trainDays
          testMeanIC testTStat testDays
          overfitFlag oosRetention oosRetentionLabel
        }
        structuralBreaks { date icBefore icAfter tStat significant }
      }
      error
    }
  }
`;

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

const GET_RESEARCH_EXPERIMENT_QUERY = `
  query GetResearchExperiment($id: Int!) {
    getResearchExperiment(id: $id) {
      id ticker featureName startDate endDate barsUsed
      meanIC icTStat icPValue adfPValue kpssPValue
      isStationary passedValidation
      monotonicityRatio isMonotonic createdAt
    }
  }
`;

const RUN_SIGNAL_ENGINE_MUTATION = `
  mutation RunSignalEngine(
    $ticker: String!
    $featureName: String! = "momentum_5m"
    $fromDate: String!
    $toDate: String!
    $flipSign: Boolean! = true
    $regimeGateEnabled: Boolean! = true
    $timespan: String! = "minute"
    $multiplier: Int! = 1
    $forceRefresh: Boolean! = false
  ) {
    runSignalEngine(
      ticker: $ticker
      featureName: $featureName
      fromDate: $fromDate
      toDate: $toDate
      flipSign: $flipSign
      regimeGateEnabled: $regimeGateEnabled
      timespan: $timespan
      multiplier: $multiplier
      forceRefresh: $forceRefresh
    ) {
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
        alphaDecay { slope intercept tStat pValue rSquared }
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
      signalBehavior {
        avgForwardReturnWhenActive skewnessActiveReturns
        avgWinReturn avgLossReturn hitRate
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
        alphaDecay { slope intercept tStat pValue rSquared }
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
      signalBehavior {
        avgForwardReturnWhenActive skewnessActiveReturns
        avgWinReturn avgLossReturn hitRate
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

interface RunResearchResponse {
  data: { runFeatureResearch: ResearchResult };
  errors?: { message: string }[];
}

interface GetExperimentsResponse {
  data: { getResearchExperiments: ResearchExperiment[] };
  errors?: { message: string }[];
}

interface GetExperimentResponse {
  data: { getResearchExperiment: ResearchExperiment | null };
  errors?: { message: string }[];
}

interface RunSignalEngineResponse {
  data: { runSignalEngine: SignalEngineResult };
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

  runFeatureResearch(input: RunFeatureResearchInput): Observable<ResearchResult> {
    return this.http
      .post<RunResearchResponse>(GRAPHQL_URL, {
        query: RUN_FEATURE_RESEARCH_MUTATION,
        variables: {
          ticker: input.ticker,
          featureName: input.featureName,
          fromDate: input.fromDate,
          toDate: input.toDate,
          timespan: input.timespan ?? 'minute',
          multiplier: input.multiplier ?? 1,
        }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.runFeatureResearch)
      );
  }

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

  runSignalEngine(input: RunSignalEngineInput): Observable<SignalEngineResult> {
    return this.http
      .post<RunSignalEngineResponse>(GRAPHQL_URL, {
        query: RUN_SIGNAL_ENGINE_MUTATION,
        variables: {
          ticker: input.ticker,
          featureName: input.featureName,
          fromDate: input.fromDate,
          toDate: input.toDate,
          flipSign: input.flipSign,
          regimeGateEnabled: input.regimeGateEnabled,
          timespan: input.timespan ?? 'minute',
          multiplier: input.multiplier ?? 1,
          forceRefresh: input.forceRefresh ?? false,
        }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.runSignalEngine)
      );
  }

  getExperiment(id: number): Observable<ResearchExperiment | null> {
    return this.http
      .post<GetExperimentResponse>(GRAPHQL_URL, {
        query: GET_RESEARCH_EXPERIMENT_QUERY,
        variables: { id }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getResearchExperiment)
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
