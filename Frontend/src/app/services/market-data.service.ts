import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { map, tap } from 'rxjs/operators';
import { Observable } from 'rxjs';
import {
  SmartAggregatesResult, CalculateIndicatorsResult, OptionsContractsResult,
  OptionsChainSnapshotResult, BacktestResult, StockSnapshotResult,
  StockSnapshotsResult, MarketMoversResult, UnifiedSnapshotResult,
  TrackedTickersResult, TickerDetailResult, RelatedTickersResult,
  StrategyAnalyzeResult, StrategyLegInput,
} from '../graphql/types';

const GRAPHQL_URL = 'http://localhost:5000/graphql';

const QUERY = `
  query GetOrFetchStockAggregates(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "day"
    $multiplier: Int! = 1
    $forceRefresh: Boolean! = false
  ) {
    getOrFetchStockAggregates(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
      forceRefresh: $forceRefresh
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
      message
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
  ) {
    runBacktest(
      ticker: $ticker
      strategyName: $strategyName
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
      parametersJson: $parametersJson
    ) {
      success id strategyName parameters
      totalTrades winningTrades losingTrades
      totalPnL maxDrawdown sharpeRatio durationMs
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

const ANALYZE_OPTIONS_STRATEGY_QUERY = `
  query AnalyzeOptionsStrategy(
    $symbol: String!
    $legs: [StrategyLegInput!]!
    $expirationDate: String!
    $spotPrice: Decimal!
    $riskFreeRate: Decimal = 0.043
  ) {
    analyzeOptionsStrategy(
      symbol: $symbol
      legs: $legs
      expirationDate: $expirationDate
      spotPrice: $spotPrice
      riskFreeRate: $riskFreeRate
    ) {
      success symbol spotPrice strategyCost
      pop expectedValue maxProfit maxLoss breakevens
      curve { price pnl }
      greeks { delta gamma theta vega }
      error
    }
  }
`;

interface AnalyzeOptionsStrategyResponse {
  data: { analyzeOptionsStrategy: StrategyAnalyzeResult };
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
    timespan: string = 'day',
    multiplier: number = 1,
    forceRefresh: boolean = false
  ): Observable<SmartAggregatesResult> {
    console.log('[STEP 1.5 - Service] Sending GraphQL query:', {
      ticker, fromDate, toDate, timespan, multiplier, forceRefresh
    });

    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: QUERY,
        variables: { ticker, fromDate, toDate, timespan, multiplier, forceRefresh }
      })
      .pipe(
        tap(response => {
          console.log('[STEP 1.7 - Service] GraphQL response:', {
            hasData: !!response.data,
            errors: response.errors,
            result: response.data?.getOrFetchStockAggregates
          });
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getOrFetchStockAggregates)
      );
  }

  checkCachedRanges(
    ticker: string,
    ranges: { fromDate: string; toDate: string }[],
    timespan: string = 'day',
    multiplier: number = 1
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
    const today = new Date().toISOString().slice(0, 10);
    const sixMonthsOut = new Date(Date.now() + 180 * 86400000).toISOString().slice(0, 10);
    return this.http
      .post<OptionsExpirationsResponse>(GRAPHQL_URL, {
        query: GET_OPTIONS_EXPIRATIONS_QUERY,
        variables: {
          underlyingTicker,
          contractType: options.contractType,
          expirationDateGte: options.expirationDateGte ?? today,
          expirationDateLte: options.expirationDateLte ?? sixMonthsOut,
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
    timespan: string = 'day',
    multiplier: number = 1
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

  getUnifiedSnapshot(tickers?: string[], limit: number = 10): Observable<UnifiedSnapshotResult> {
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
    timespan: string = 'minute',
    multiplier: number = 1,
    parametersJson: string = '{}'
  ): Observable<BacktestResult> {
    return this.http
      .post<RunBacktestResponse>(GRAPHQL_URL, {
        query: RUN_BACKTEST_MUTATION,
        variables: { ticker, strategyName, fromDate, toDate, timespan, multiplier, parametersJson }
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

  analyzeOptionsStrategy(
    symbol: string,
    legs: StrategyLegInput[],
    expirationDate: string,
    spotPrice: number,
    riskFreeRate: number = 0.043
  ): Observable<StrategyAnalyzeResult> {
    return this.http
      .post<AnalyzeOptionsStrategyResponse>(GRAPHQL_URL, {
        query: ANALYZE_OPTIONS_STRATEGY_QUERY,
        variables: { symbol, legs, expirationDate, spotPrice, riskFreeRate }
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
}
