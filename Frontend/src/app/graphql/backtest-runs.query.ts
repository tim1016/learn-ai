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
