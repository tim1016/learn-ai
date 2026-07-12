import { gql } from "apollo-angular";
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
      equityCurveJson
      equityCurve {
        t
        e
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
  verdictGrade?: string | null;
  verdictSignal?: string | null;
  parityGroupId?: string | null;
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
  sharpeRatio: number;
  sortinoRatio: number;
  profitFactor: number;
  leanStatisticsJson: string | null;
  verdictJson: string | null;
  verdictVersion: number | null;
  verdictGrade: string | null;
  verdictSignal: string | null;
  equityCurveJson: string | null;
  equityCurve: { t: number; e: number }[];
  insightSummaryJson: string | null;
  parityGroupId: string | null;
  trades: BacktestRunDetailTrade[];
  parityVerdicts: { id: number; status: string; verdictJson: string; createdAt: number }[];
}

export interface BacktestRunDetailTrade {
  id: number;
  entryTimestamp: number;
  exitTimestamp: number;
  entryPrice: number;
  exitPrice: number;
  quantity: number;
  pnL: number;
  signalReason: string;
  isSyntheticExit: boolean;
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
    verdictGrade: node.verdictGrade ?? null,
    verdictSignal: node.verdictSignal ?? null,
    parityGroupId: node.parityGroupId ?? null,
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
