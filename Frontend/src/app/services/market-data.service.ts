import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { map, tap } from 'rxjs/operators';
import { Observable } from 'rxjs';
import {
  SmartAggregatesResult, CalculateIndicatorsResult, OptionsContractsResult,
  OptionsChainSnapshotResult, BacktestResult, StockSnapshotResult,
  StockSnapshotsResult, MarketMoversResult, UnifiedSnapshotResult,
  TrackedTickersResult, TickerDetailResult, RelatedTickersResult,
  StrategyAnalyzeResult, StrategyAnalyzeOptions, StrategyLegInput, FetchProgress,
  IndicatorTableResult, RuleBasedBacktestResult, PricingCompareResult,
} from '../graphql/types';
import { environment } from '../../environments/environment';
import { todayDateString, dateStringMonthsFromNow } from '../utils/date-validation';

const GRAPHQL_URL = environment.backendUrl;

const QUERY = `
  query GetOrFetchStockAggregates(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "day"
    $multiplier: Int! = 1
    $forceRefresh: Boolean! = false
    $adjusted: Boolean! = true
  ) {
    getOrFetchStockAggregates(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
      forceRefresh: $forceRefresh
      adjusted: $adjusted
    ) {
      ticker
      aggregates {
        id open high low close volume
        volumeWeightedAveragePrice timestamp
        timespan multiplier transactionCount
      }
      summary {
        periodHigh periodLow averageVolume averageVwap
        openPrice closePrice priceChange priceChangePercent totalBars
      }
      gapDetection {
        totalWeekdays daysWithData missingDays partialDays
        coveragePercent expectedBars actualBars
        missingDates partialDates
      }
    }
  }
`;

interface GraphQLResponse {
  data: { getOrFetchStockAggregates: SmartAggregatesResult };
  errors?: { message: string }[];
}

const CALCULATE_INDICATORS_QUERY = `
  query CalculateIndicators(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $indicators: [IndicatorConfigInput!]!
    $timespan: String! = "day"
    $multiplier: Int! = 1
  ) {
    calculateIndicators(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      indicators: $indicators
      timespan: $timespan
      multiplier: $multiplier
    ) {
      success
      ticker
      indicators {
        name
        window
        data {
          timestamp value signal histogram upper lower
        }
      }
      error
    }
  }
`;

const CHECK_CACHED_RANGES_QUERY = `
  query CheckCachedRanges(
    $ticker: String!
    $ranges: [DateRangeInput!]!
    $timespan: String! = "day"
    $multiplier: Int! = 1
  ) {
    checkCachedRanges(
      ticker: $ticker
      ranges: $ranges
      timespan: $timespan
      multiplier: $multiplier
    ) {
      fromDate toDate isCached
    }
  }
`;

interface CheckCachedRangesResponse {
  data: { checkCachedRanges: CachedRangeResult[] };
  errors?: { message: string }[];
}

export interface CachedRangeResult {
  fromDate: string;
  toDate: string;
  isCached: boolean;
}

interface CalculateIndicatorsResponse {
  data: { calculateIndicators: CalculateIndicatorsResult };
  errors?: { message: string }[];
}

const GET_OPTIONS_CONTRACTS_QUERY = `
  query GetOptionsContracts(
    $underlyingTicker: String!
    $asOfDate: String
    $contractType: String
    $strikePriceGte: Decimal
    $strikePriceLte: Decimal
    $expirationDate: String
    $expirationDateGte: String
    $expirationDateLte: String
    $limit: Int! = 100
  ) {
    getOptionsContracts(
      underlyingTicker: $underlyingTicker
      asOfDate: $asOfDate
      contractType: $contractType
      strikePriceGte: $strikePriceGte
      strikePriceLte: $strikePriceLte
      expirationDate: $expirationDate
      expirationDateGte: $expirationDateGte
      expirationDateLte: $expirationDateLte
      limit: $limit
    ) {
      success
      contracts {
        ticker underlyingTicker contractType
        strikePrice expirationDate exerciseStyle
      }
      count
      error
    }
  }
`;

interface OptionsContractsResponse {
  data: { getOptionsContracts: OptionsContractsResult };
  errors?: { message: string }[];
}

const GET_OPTIONS_EXPIRATIONS_QUERY = `
  query GetOptionsExpirations(
    $underlyingTicker: String!
    $contractType: String
    $expirationDateGte: String
    $expirationDateLte: String
  ) {
    getOptionsExpirations(
      underlyingTicker: $underlyingTicker
      contractType: $contractType
      expirationDateGte: $expirationDateGte
      expirationDateLte: $expirationDateLte
    ) {
      success
      expirations
      count
      error
    }
  }
`;

interface OptionsExpirationsResponse {
  data: { getOptionsExpirations: { success: boolean; expirations: string[]; count: number; error?: string } };
  errors?: { message: string }[];
}

const GET_OPTIONS_CHAIN_SNAPSHOT_QUERY = `
  query GetOptionsChainSnapshot($underlyingTicker: String!, $expirationDate: String) {
    getOptionsChainSnapshot(underlyingTicker: $underlyingTicker, expirationDate: $expirationDate) {
      success
      underlying {
        ticker price change changePercent
      }
      contracts {
        ticker contractType strikePrice expirationDate
        breakEvenPrice impliedVolatility openInterest
        greeks { delta gamma theta vega }
        day { open high low close volume vwap }
        lastTrade { price size exchange timeframe }
        lastQuote { bid ask bidSize askSize midpoint timeframe }
      }
      count
      error
    }
  }
`;

interface OptionsChainSnapshotResponse {
  data: { getOptionsChainSnapshot: OptionsChainSnapshotResult };
  errors?: { message: string }[];
}

const SNAPSHOT_FIELDS = `
  ticker
  day { open high low close volume vwap }
  prevDay { open high low close volume vwap }
  min { open high low close volume vwap accumulatedVolume timestamp }
  todaysChange todaysChangePercent updated
`;

const GET_STOCK_SNAPSHOT_QUERY = `
  query GetStockSnapshot($ticker: String!) {
    getStockSnapshot(ticker: $ticker) {
      success
      snapshot { ${SNAPSHOT_FIELDS} }
      error
    }
  }
`;

interface StockSnapshotResponse {
  data: { getStockSnapshot: StockSnapshotResult };
  errors?: { message: string }[];
}

const GET_STOCK_SNAPSHOTS_QUERY = `
  query GetStockSnapshots($tickers: [String!]) {
    getStockSnapshots(tickers: $tickers) {
      success
      snapshots { ${SNAPSHOT_FIELDS} }
      count
      error
    }
  }
`;

interface StockSnapshotsResponse {
  data: { getStockSnapshots: StockSnapshotsResult };
  errors?: { message: string }[];
}

const GET_MARKET_MOVERS_QUERY = `
  query GetMarketMovers($direction: String!) {
    getMarketMovers(direction: $direction) {
      success
      tickers { ${SNAPSHOT_FIELDS} }
      count
      error
    }
  }
`;

interface MarketMoversResponseGql {
  data: { getMarketMovers: MarketMoversResult };
  errors?: { message: string }[];
}

const GET_UNIFIED_SNAPSHOT_QUERY = `
  query GetUnifiedSnapshot($tickers: [String!], $limit: Int! = 10) {
    getUnifiedSnapshot(tickers: $tickers, limit: $limit) {
      success
      results {
        ticker type marketStatus name
        session {
          price change changePercent
          open close high low previousClose volume
        }
      }
      count
      error
    }
  }
`;

interface UnifiedSnapshotResponseGql {
  data: { getUnifiedSnapshot: UnifiedSnapshotResult };
  errors?: { message: string }[];
}

// Ticker Reference queries
const GET_TRACKED_TICKERS_QUERY = `
  query GetTrackedTickers($tickers: [String!]!) {
    getTrackedTickers(tickers: $tickers) {
      success
      tickers {
        ticker name market type active
        primaryExchange currencyName
      }
      count
      error
    }
  }
`;

interface TrackedTickersResponseGql {
  data: { getTrackedTickers: TrackedTickersResult };
  errors?: { message: string }[];
}

const GET_TICKER_DETAILS_QUERY = `
  query GetTickerDetails($ticker: String!) {
    getTickerDetails(ticker: $ticker) {
      success ticker name description
      marketCap homepageUrl totalEmployees
      listDate sicDescription primaryExchange
      type weightedSharesOutstanding
      address { address1 city state postalCode }
      error
    }
  }
`;

interface TickerDetailResponseGql {
  data: { getTickerDetails: TickerDetailResult };
  errors?: { message: string }[];
}

const GET_RELATED_TICKERS_QUERY = `
  query GetRelatedTickers($ticker: String!) {
    getRelatedTickers(ticker: $ticker) {
      success ticker related error
    }
  }
`;

interface RelatedTickersResponseGql {
  data: { getRelatedTickers: RelatedTickersResult };
  errors?: { message: string }[];
}

const RUN_BACKTEST_MUTATION = `
  mutation RunBacktest(
    $ticker: String!
    $strategyName: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "minute"
    $multiplier: Int! = 1
    $parametersJson: String! = "{}"
    $filterRth: Boolean! = true
  ) {
    runBacktest(
      ticker: $ticker
      strategyName: $strategyName
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
      parametersJson: $parametersJson
      filterRth: $filterRth
    ) {
      success id strategyName parameters
      totalTrades winningTrades losingTrades
      totalPnL maxDrawdown sharpeRatio durationMs
      sourceBars rthBars resampledBars timeframe
      trades {
        tradeType entryTimestamp exitTimestamp
        entryPrice exitPrice pnl cumulativePnl signalReason
      }
      error
    }
  }
`;

interface RunBacktestResponse {
  data: { runBacktest: BacktestResult };
  errors?: { message: string }[];
}

const RUN_BACKTEST_FROM_CSV_MUTATION = `
  mutation RunBacktestFromCsvBars(
    $strategyName: String!
    $parametersJson: String! = "{}"
    $bars: [CsvBarInputInput!]!
    $filterRth: Boolean! = false
  ) {
    runBacktestFromCsvBars(
      strategyName: $strategyName
      parametersJson: $parametersJson
      bars: $bars
      filterRth: $filterRth
    ) {
      success id strategyName parameters
      totalTrades winningTrades losingTrades
      totalPnL maxDrawdown sharpeRatio durationMs
      sourceBars rthBars resampledBars timeframe
      trades {
        tradeType entryTimestamp exitTimestamp
        entryPrice exitPrice pnl cumulativePnl signalReason
      }
      error
    }
  }
`;

interface RunBacktestFromCsvResponse {
  data: { runBacktestFromCsvBars: BacktestResult };
  errors?: { message: string }[];
}

const RUN_RULE_BASED_BACKTEST_MUTATION = `
  mutation RunRuleBasedBacktest(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $multiplier: Int! = 15
    $timespan: String! = "minute"
    $filterRth: Boolean! = true
    $parametersJson: String! = "{}"
  ) {
    runRuleBasedBacktest(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      multiplier: $multiplier
      timespan: $timespan
      filterRth: $filterRth
      parametersJson: $parametersJson
    ) {
      success ticker strategyName parameters
      totalTrades winningTrades losingTrades
      winRate avgWinPct avgLossPct
      winLossRatio profitFactor expectancyPerTrade
      totalPnlPct maxDrawdownPct totalPnlPts
      sharpeRatio barsProcessed
      trades {
        tradeNumber tradeType
        entryTimestamp exitTimestamp
        entryPrice exitPrice
        pnl pnlPct cumulativePnlPct
        signalReason
        emaFast emaSlow emaGap rsi adx
      }
      error
    }
  }
`;

interface RunRuleBasedBacktestResponse {
  data: { runRuleBasedBacktest: RuleBasedBacktestResult };
  errors?: { message: string }[];
}

const GET_FETCH_PROGRESS_QUERY = `
  query GetFetchProgress($ticker: String!) {
    getFetchProgress(ticker: $ticker) {
      ticker totalWindows completedWindows
      barsFetched currentWindow status
    }
  }
`;

interface FetchProgressResponse {
  data: { getFetchProgress: FetchProgress | null };
  errors?: { message: string }[];
}

const ANALYZE_OPTIONS_STRATEGY_QUERY = `
  query AnalyzeOptionsStrategy(
    $symbol: String!
    $legs: [StrategyLegInput!]!
    $expirationDate: String!
    $spotPrice: Decimal!
    $riskFreeRate: Decimal = 0.043
    $includeCurrentCurve: Boolean = false
    $includeGreekCurves: Boolean = false
    $includeLegDiagnostics: Boolean = false
    $whatIfTimeShiftDays: Decimal = 0
    $whatIfIvShift: Decimal = 0
  ) {
    analyzeOptionsStrategy(
      symbol: $symbol
      legs: $legs
      expirationDate: $expirationDate
      spotPrice: $spotPrice
      riskFreeRate: $riskFreeRate
      includeCurrentCurve: $includeCurrentCurve
      includeGreekCurves: $includeGreekCurves
      includeLegDiagnostics: $includeLegDiagnostics
      whatIfTimeShiftDays: $whatIfTimeShiftDays
      whatIfIvShift: $whatIfIvShift
    ) {
      success symbol spotPrice strategyCost
      pop expectedValue maxProfit maxLoss breakevens
      curve { price pnl }
      greeks { delta gamma theta vega }
      currentCurve { price theoreticalValue theoreticalPnl }
      greekCurves { price delta gamma theta vega }
      legDiagnostics {
        legId strike optionType position quantity iv entryPremium
        currentTheoretical currentDelta currentGamma currentTheta currentVega
      }
      error
    }
  }
`;

interface AnalyzeOptionsStrategyResponse {
  data: { analyzeOptionsStrategy: StrategyAnalyzeResult };
  errors?: { message: string }[];
}

const PRICING_MODEL_COMPARISON_QUERY = `
  query PricingModelComparison(
    $spot: Decimal!
    $strike: Decimal!
    $volatility: Decimal!
    $expirationDate: String!
    $optionType: String!
    $riskFreeRate: Decimal = 0.05
    $dividendYield: Decimal = 0
    $evaluationDate: String
    $spotMin: Decimal
    $spotMax: Decimal
    $numPoints: Int = 100
  ) {
    pricingModelComparison(
      spot: $spot
      strike: $strike
      volatility: $volatility
      expirationDate: $expirationDate
      optionType: $optionType
      riskFreeRate: $riskFreeRate
      dividendYield: $dividendYield
      evaluationDate: $evaluationDate
      spotMin: $spotMin
      spotMax: $spotMax
      numPoints: $numPoints
    ) {
      success
      strike
      optionType
      expirationDate
      timeToExpiryYears
      models {
        model
        points { spot price delta gamma theta vega rho }
      }
      error
    }
  }
`;

interface PricingModelComparisonResponse {
  data: { pricingModelComparison: PricingCompareResult };
  errors?: { message: string }[];
}

const GENERATE_INDICATOR_TABLE_QUERY = `
  query GenerateIndicatorTable(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $multiplier: Int! = 1
    $timespan: String! = "minute"
    $emaPeriods: [Int!]
    $bbLength: Int! = 20
    $bbStd: Float! = 2.0
    $supertrendLength: Int! = 10
    $supertrendMultiplier: Float! = 3.0
    $rsiLength: Int! = 14
    $rsiMaLength: Int! = 14
    $macdFast: Int! = 12
    $macdSlow: Int! = 26
    $macdSignal: Int! = 9
    $adxLength: Int! = 14
  ) {
    generateIndicatorTable(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      multiplier: $multiplier
      timespan: $timespan
      emaPeriods: $emaPeriods
      bbLength: $bbLength
      bbStd: $bbStd
      supertrendLength: $supertrendLength
      supertrendMultiplier: $supertrendMultiplier
      rsiLength: $rsiLength
      rsiMaLength: $rsiMaLength
      macdFast: $macdFast
      macdSlow: $macdSlow
      macdSignal: $macdSignal
      adxLength: $adxLength
    ) {
      success ticker rowCount columns rows error
    }
  }
`;

interface GenerateIndicatorTableResponse {
  data: { generateIndicatorTable: IndicatorTableResult };
  errors?: { message: string }[];
}

@Injectable({
  providedIn: 'root'
})
export class MarketDataService {
  private http = inject(HttpClient);

  getOrFetchStockAggregates(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan = 'day',
    multiplier = 1,
    forceRefresh = false,
    adjusted = true
  ): Observable<SmartAggregatesResult> {
    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: QUERY,
        variables: { ticker, fromDate, toDate, timespan, multiplier, forceRefresh, adjusted }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getOrFetchStockAggregates)
      );
  }

  getFetchProgress(ticker: string): Observable<FetchProgress | null> {
    return this.http
      .post<FetchProgressResponse>(GRAPHQL_URL, {
        query: GET_FETCH_PROGRESS_QUERY,
        variables: { ticker: ticker.toUpperCase() }
      })
      .pipe(
        map(response => response.data.getFetchProgress)
      );
  }

  checkCachedRanges(
    ticker: string,
    ranges: { fromDate: string; toDate: string }[],
    timespan = 'day',
    multiplier = 1
  ): Observable<CachedRangeResult[]> {
    return this.http
      .post<CheckCachedRangesResponse>(GRAPHQL_URL, {
        query: CHECK_CACHED_RANGES_QUERY,
        variables: { ticker, ranges, timespan, multiplier }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.checkCachedRanges)
      );
  }

  getOptionsContracts(
    underlyingTicker: string,
    options: {
      asOfDate?: string;
      contractType?: string;
      strikePriceGte?: number;
      strikePriceLte?: number;
      expirationDate?: string;
      expirationDateGte?: string;
      expirationDateLte?: string;
      limit?: number;
    } = {}
  ): Observable<OptionsContractsResult> {
    return this.http
      .post<OptionsContractsResponse>(GRAPHQL_URL, {
        query: GET_OPTIONS_CONTRACTS_QUERY,
        variables: { underlyingTicker, ...options }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getOptionsContracts)
      );
  }

  getOptionsExpirations(
    underlyingTicker: string,
    options: {
      contractType?: string;
      expirationDateGte?: string;
      expirationDateLte?: string;
    } = {}
  ): Observable<string[]> {
    return this.http
      .post<OptionsExpirationsResponse>(GRAPHQL_URL, {
        query: GET_OPTIONS_EXPIRATIONS_QUERY,
        variables: {
          underlyingTicker,
          contractType: options.contractType,
          expirationDateGte: options.expirationDateGte ?? todayDateString(),
          expirationDateLte: options.expirationDateLte ?? dateStringMonthsFromNow(6),
        }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => {
          const result = response.data.getOptionsExpirations;
          return result.success ? result.expirations : [];
        })
      );
  }

  calculateIndicators(
    ticker: string,
    fromDate: string,
    toDate: string,
    indicators: { name: string; window: number }[],
    timespan = 'day',
    multiplier = 1
  ): Observable<CalculateIndicatorsResult> {
    return this.http
      .post<CalculateIndicatorsResponse>(GRAPHQL_URL, {
        query: CALCULATE_INDICATORS_QUERY,
        variables: { ticker, fromDate, toDate, indicators, timespan, multiplier }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.calculateIndicators)
      );
  }

  getOptionsChainSnapshot(
    underlyingTicker: string,
    expirationDate?: string
  ): Observable<OptionsChainSnapshotResult> {
    return this.http
      .post<OptionsChainSnapshotResponse>(GRAPHQL_URL, {
        query: GET_OPTIONS_CHAIN_SNAPSHOT_QUERY,
        variables: { underlyingTicker, expirationDate }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getOptionsChainSnapshot)
      );
  }

  getStockSnapshot(ticker: string): Observable<StockSnapshotResult> {
    return this.http
      .post<StockSnapshotResponse>(GRAPHQL_URL, {
        query: GET_STOCK_SNAPSHOT_QUERY,
        variables: { ticker }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getStockSnapshot)
      );
  }

  getStockSnapshots(tickers?: string[]): Observable<StockSnapshotsResult> {
    return this.http
      .post<StockSnapshotsResponse>(GRAPHQL_URL, {
        query: GET_STOCK_SNAPSHOTS_QUERY,
        variables: { tickers }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getStockSnapshots)
      );
  }

  getMarketMovers(direction: string): Observable<MarketMoversResult> {
    return this.http
      .post<MarketMoversResponseGql>(GRAPHQL_URL, {
        query: GET_MARKET_MOVERS_QUERY,
        variables: { direction }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getMarketMovers)
      );
  }

  getUnifiedSnapshot(tickers?: string[], limit = 10): Observable<UnifiedSnapshotResult> {
    return this.http
      .post<UnifiedSnapshotResponseGql>(GRAPHQL_URL, {
        query: GET_UNIFIED_SNAPSHOT_QUERY,
        variables: { tickers, limit }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getUnifiedSnapshot)
      );
  }

  getTrackedTickers(tickers: string[]): Observable<TrackedTickersResult> {
    return this.http
      .post<TrackedTickersResponseGql>(GRAPHQL_URL, {
        query: GET_TRACKED_TICKERS_QUERY,
        variables: { tickers }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getTrackedTickers)
      );
  }

  getTickerDetails(ticker: string): Observable<TickerDetailResult> {
    return this.http
      .post<TickerDetailResponseGql>(GRAPHQL_URL, {
        query: GET_TICKER_DETAILS_QUERY,
        variables: { ticker }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getTickerDetails)
      );
  }

  getRelatedTickers(ticker: string): Observable<RelatedTickersResult> {
    return this.http
      .post<RelatedTickersResponseGql>(GRAPHQL_URL, {
        query: GET_RELATED_TICKERS_QUERY,
        variables: { ticker }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getRelatedTickers)
      );
  }

  runBacktest(
    ticker: string,
    strategyName: string,
    fromDate: string,
    toDate: string,
    timespan = 'minute',
    multiplier = 1,
    parametersJson = '{}',
    filterRth = true
  ): Observable<BacktestResult> {
    return this.http
      .post<RunBacktestResponse>(GRAPHQL_URL, {
        query: RUN_BACKTEST_MUTATION,
        variables: { ticker, strategyName, fromDate, toDate, timespan, multiplier, parametersJson, filterRth }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.runBacktest)
      );
  }

  runBacktestFromCsvBars(
    strategyName: string,
    bars: { timestamp: number; open: number; high: number; low: number; close: number; volume: number }[],
    parametersJson = '{}',
    filterRth = false
  ): Observable<BacktestResult> {
    return this.http
      .post<RunBacktestFromCsvResponse>(GRAPHQL_URL, {
        query: RUN_BACKTEST_FROM_CSV_MUTATION,
        variables: { strategyName, bars, parametersJson, filterRth }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.runBacktestFromCsvBars)
      );
  }

  runRuleBasedBacktest(
    ticker: string,
    fromDate: string,
    toDate: string,
    multiplier = 15,
    timespan = 'minute',
    filterRth = true,
    parametersJson = '{}',
  ): Observable<RuleBasedBacktestResult> {
    return this.http
      .post<RunRuleBasedBacktestResponse>(GRAPHQL_URL, {
        query: RUN_RULE_BASED_BACKTEST_MUTATION,
        variables: { ticker, fromDate, toDate, multiplier, timespan, filterRth, parametersJson }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.runRuleBasedBacktest)
      );
  }

  generateIndicatorTable(
    ticker: string,
    fromDate: string,
    toDate: string,
    options: {
      multiplier?: number;
      timespan?: string;
      emaPeriods?: number[];
      bbLength?: number;
      bbStd?: number;
      supertrendLength?: number;
      supertrendMultiplier?: number;
      rsiLength?: number;
      rsiMaLength?: number;
      macdFast?: number;
      macdSlow?: number;
      macdSignal?: number;
      adxLength?: number;
    } = {}
  ): Observable<IndicatorTableResult> {
    return this.http
      .post<GenerateIndicatorTableResponse>(GRAPHQL_URL, {
        query: GENERATE_INDICATOR_TABLE_QUERY,
        variables: { ticker, fromDate, toDate, ...options }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.generateIndicatorTable)
      );
  }

  analyzeOptionsStrategy(
    symbol: string,
    legs: StrategyLegInput[],
    expirationDate: string,
    spotPrice: number,
    riskFreeRate = 0.043,
    options: StrategyAnalyzeOptions = {},
  ): Observable<StrategyAnalyzeResult> {
    return this.http
      .post<AnalyzeOptionsStrategyResponse>(GRAPHQL_URL, {
        query: ANALYZE_OPTIONS_STRATEGY_QUERY,
        variables: {
          symbol, legs, expirationDate, spotPrice, riskFreeRate,
          includeCurrentCurve: options.includeCurrentCurve ?? false,
          includeGreekCurves: options.includeGreekCurves ?? false,
          includeLegDiagnostics: options.includeLegDiagnostics ?? false,
          whatIfTimeShiftDays: options.whatIfTimeShiftDays ?? 0,
          whatIfIvShift: options.whatIfIvShift ?? 0,
        }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.analyzeOptionsStrategy)
      );
  }

  comparePricingModels(params: {
    spot: number;
    strike: number;
    volatility: number;
    expirationDate: string;
    optionType: string;
    riskFreeRate?: number;
    dividendYield?: number;
    evaluationDate?: string;
    spotMin?: number;
    spotMax?: number;
    numPoints?: number;
  }): Observable<PricingCompareResult> {
    return this.http
      .post<PricingModelComparisonResponse>(GRAPHQL_URL, {
        query: PRICING_MODEL_COMPARISON_QUERY,
        variables: {
          spot: params.spot,
          strike: params.strike,
          volatility: params.volatility,
          expirationDate: params.expirationDate,
          optionType: params.optionType,
          riskFreeRate: params.riskFreeRate ?? 0.05,
          dividendYield: params.dividendYield ?? 0,
          evaluationDate: params.evaluationDate ?? null,
          spotMin: params.spotMin ?? null,
          spotMax: params.spotMax ?? null,
          numPoints: params.numPoints ?? 100,
        },
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.pricingModelComparison),
      );
  }
}
