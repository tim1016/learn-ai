import { gql } from "apollo-angular";
import type { EngineValidationAnalytics } from "../components/lean-engine/engine-results/engine-validation-analytics.types";
import { RunHistoryRow } from "../components/shared/run-history/run-history.types";
import type { DataPolicy } from "../models/data-policy";

export const BACKTEST_RUNS_QUERY = gql`
  query BacktestRuns($engine: Engine, $symbol: String, $first: Int, $after: String) {
    backtestRuns(engine: $engine, symbol: $symbol, first: $first, after: $after) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        source
        engine
        strategyName
        leanRunId
        parameters
        startDate
        endDate
        executedAt
        totalTrades
        totalPnL
        commissionPerOrder
        brokeragePolicy
        verdictGrade
        verdictSignal
        parityGroupId
        notes
        dataPolicy {
          source
          symbol
          adjusted
          session
          # PR B.3 — alias server-side camelCase to the canonical snake_case
          # DataPolicy wire contract (see Frontend/src/app/models/data-policy.ts
          # and spec § 6.1). Consumers (RunHistoryComponent.barsSummary,
          # CSV export) read these as snake_case; without aliases the response
          # would arrive as inputBars/strategyBars and crash on dereference.
          input_bars: inputBars {
            timespan
            multiplier
          }
          strategy_bars: strategyBars {
            timespan
            multiplier
          }
          timestamp_policy: timestampPolicy
          timezone
          provider_kind: providerKind
          fixture_id: fixtureId
          fixture_sha256: fixtureSha256
        }
        trades {
          isSyntheticExit
        }
      }
    }
  }
`;

export const UPDATE_BACKTEST_RUN_NOTES_MUTATION = gql`
  mutation UpdateBacktestRunNotes($id: Int!, $notes: String!) {
    updateBacktestRunNotes(id: $id, notes: $notes) {
      id
      notes
    }
  }
`;

export const BACKTEST_RUN_DETAIL_QUERY = gql`
  query BacktestRunDetail($id: Int!) {
    backtestRun(id: $id) {
      id
      engine
      source
      strategyName
      symbol
      leanRunId
      startDate
      endDate
      fillMode
      executedAt
      durationMs
      totalTrades
      winningTrades
      losingTrades
      winRate
      totalPnL
      initialCash
      finalEquity
      totalFees
      maxDrawdown
      sharpeRatio
      sortinoRatio
      profitFactor
      leanStatisticsJson
      verdictJson
      verdictVersion
      verdictGrade
      verdictSignal
      equityCurve {
        cadence
        rawPoints
        keptPoints
        error
        points {
          t
          e
        }
      }
      # Frozen validation-analytics envelope. Aliased to the canonical
      # snake_case wire shape (EngineValidationAnalytics) — same PR B.3
      # convention as the dataPolicy block below.
      validationAnalytics {
        schemaVersion
        computedAtMs
        engine
        error
        horizons {
          key
          label
          start_ms_utc: startMsUtc
          end_ms_utc: endMsUtc
          has_full_coverage: hasFullCoverage
          net_return: netReturn
          trade_count: tradeCount
          win_rate: winRate
          profit_factor: profitFactor
        }
        timing_cells: timingCells {
          weekday
          weekday_label: weekdayLabel
          hour_et: hourEt
          trade_count: tradeCount
          win_rate: winRate
          average_return: averageReturn
        }
        seasonality {
          month
          month_label: monthLabel
          observation_count: observationCount
          median_compounded_return: medianCompoundedReturn
        }
        rolling_trade_stability: rollingTradeStability {
          trade_number: tradeNumber
          end_ms_utc: endMsUtc
          window_size: windowSize
          average_return: averageReturn
          win_rate: winRate
        }
      }
      # DataPolicy is the key the run report uses to re-fetch chart bars
      # from the shared bar store (symbol/adjusted/session/timeframe).
      dataPolicy {
        source
        symbol
        adjusted
        session
        input_bars: inputBars {
          timespan
          multiplier
        }
        strategy_bars: strategyBars {
          timespan
          multiplier
        }
        timestamp_policy: timestampPolicy
        timezone
        provider_kind: providerKind
        fixture_id: fixtureId
        fixture_sha256: fixtureSha256
      }
      insightSummaryJson
      parityGroupId
      trades {
        id
        entryTimestamp
        exitTimestamp
        entryPrice
        exitPrice
        quantity
        pnL
        pnlPts
        pnlPct
        signalReason
        isSyntheticExit
      }
      parityVerdicts {
        id
        status
        verdictJson
        createdAt
      }
    }
  }
`;

export interface BacktestRunNode {
  id: number;
  source: "engine" | "strategy-lab" | "lean-sidecar";
  /** PR B (2026-05-19) — unified engine identity (PYTHON | LEAN). */
  engine: Engine;
  strategyName: string;
  leanRunId: string | null;
  parameters: string | null;
  startDate: string;
  endDate: string;
  executedAt: number;
  totalTrades: number;
  totalPnL: number;
  /** PR B — commission per order recorded at persist time. Null on legacy rows. */
  commissionPerOrder: number | null;
  /** PR B — brokerage policy ("algorithm_default" / IB / etc.). Null on legacy rows. */
  brokeragePolicy: string | null;
  verdictGrade: string | null;
  verdictSignal: string | null;
  parityGroupId: string | null;
  /** Free-text researcher notes. Edited via the updateBacktestRunNotes mutation. */
  notes: string | null;
  /** PR B — canonical DataPolicy block. Null on legacy rows (predate the column). */
  dataPolicy: DataPolicy | null;
  trades: { isSyntheticExit: boolean }[];
}

export interface BacktestRunsConnection {
  pageInfo: { hasNextPage: boolean; endCursor: string | null };
  nodes: BacktestRunNode[];
}

export interface BacktestRunsQueryResult {
  backtestRuns: BacktestRunsConnection;
}

export interface BacktestRunDetail {
  id: number;
  engine: Engine;
  source: "engine" | "strategy-lab" | "lean-sidecar";
  strategyName: string;
  symbol: string;
  leanRunId: string | null;
  startDate: string;
  endDate: string;
  fillMode: string;
  executedAt: number;
  durationMs: number;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  totalPnL: number;
  initialCash: number;
  finalEquity: number;
  totalFees: number;
  maxDrawdown: number;
  sharpeRatio: number | null;
  sortinoRatio: number | null;
  profitFactor: number | null;
  leanStatisticsJson: string | null;
  verdictJson: string | null;
  verdictVersion: number | null;
  verdictGrade: string | null;
  verdictSignal: string | null;
  equityCurve: BacktestRunEquityCurve | null;
  validationAnalytics: BacktestRunValidationAnalytics | null;
  /** Canonical DataPolicy block — the run report's key for re-fetching chart bars. */
  dataPolicy: DataPolicy | null;
  insightSummaryJson: string | null;
  parityGroupId: string | null;
  trades: BacktestRunDetailTrade[];
  parityVerdicts: { id: number; status: string; verdictJson: string; createdAt: number }[];
}

/** Frozen validation-analytics envelope: canonical snake_case analytics
 *  body (EngineValidationAnalytics) plus envelope metadata. */
export interface BacktestRunValidationAnalytics extends EngineValidationAnalytics {
  schemaVersion: number;
  computedAtMs: number;
  engine: string;
  error: string | null;
}

export interface BacktestRunDetailTrade {
  id: number;
  entryTimestamp: number;
  exitTimestamp: number;
  entryPrice: number;
  exitPrice: number;
  quantity: number;
  pnL: number;
  pnlPts: number;
  pnlPct: number;
  signalReason: string;
  isSyntheticExit: boolean;
}

export interface BacktestRunEquityCurve {
  cadence: string | null;
  rawPoints: number;
  keptPoints: number;
  error: string | null;
  points: { t: number; e: number }[];
}

export interface BacktestRunDetailQueryResult {
  backtestRun: BacktestRunDetail | null;
}

/** PR B (2026-05-19) — unified engine identity used by the GraphQL filter. */
export type Engine = "PYTHON" | "LEAN";

export function toRunHistoryRow(node: BacktestRunNode): RunHistoryRow {
  return {
    id: String(node.id),
    source: node.source,
    engine: node.engine,
    strategyName: node.strategyName,
    symbol: extractSymbol(node.parameters),
    startDate: node.startDate,
    endDate: node.endDate,
    executedAt: node.executedAt,
    totalTrades: node.totalTrades,
    totalPnl: node.totalPnL,
    hasSyntheticExit: node.trades.some((t) => t.isSyntheticExit),
    leanRunId: node.leanRunId,
    dataPolicy: node.dataPolicy,
    notes: node.notes,
    commissionPerOrder: node.commissionPerOrder,
    brokeragePolicy: node.brokeragePolicy,
    verdictGrade: node.verdictGrade,
    verdictSignal: node.verdictSignal,
    parityGroupId: node.parityGroupId,
  };
}

function extractSymbol(parameters: string | null): string | null {
  if (!parameters) return null;
  try {
    const parsed = JSON.parse(parameters) as { symbol?: string };
    return parsed.symbol ?? null;
  } catch (error) {
    console.warn("extractSymbol: failed to parse parameters JSON", {
      context: "extractSymbol",
      parameters,
      error,
    });
    return null;
  }
}
