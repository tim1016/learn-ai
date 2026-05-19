import { gql } from "apollo-angular";
import { RunHistoryRow } from "../components/shared/run-history/run-history.types";

export const BACKTEST_RUNS_QUERY = gql`
  query BacktestRuns($engine: EngineSource, $symbol: String, $first: Int, $after: String) {
    backtestRuns(engine: $engine, symbol: $symbol, first: $first, after: $after) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        source
        strategyName
        leanRunId
        parameters
        startDate
        endDate
        executedAt
        totalTrades
        totalPnL
        trades {
          isSyntheticExit
        }
      }
    }
  }
`;

export interface BacktestRunNode {
  id: string;
  source: "engine" | "strategy-lab" | "lean-sidecar";
  strategyName: string;
  leanRunId: string | null;
  parameters: string | null;
  startDate: string;
  endDate: string;
  executedAt: string;
  totalTrades: number;
  totalPnL: number;
  trades: { isSyntheticExit: boolean }[];
}

export interface BacktestRunsConnection {
  pageInfo: { hasNextPage: boolean; endCursor: string | null };
  nodes: BacktestRunNode[];
}

export interface BacktestRunsQueryResult {
  backtestRuns: BacktestRunsConnection;
}

export type EngineSource = "ENGINE" | "STRATEGY_LAB" | "LEAN_SIDECAR";

export function toRunHistoryRow(node: BacktestRunNode): RunHistoryRow {
  return {
    id: node.id,
    source: node.source,
    strategyName: node.strategyName,
    symbol: extractSymbol(node.parameters),
    startDate: node.startDate,
    endDate: node.endDate,
    executedAt: node.executedAt,
    totalTrades: node.totalTrades,
    totalPnl: node.totalPnL,
    hasSyntheticExit: node.trades.some((t) => t.isSyntheticExit),
    leanRunId: node.leanRunId,
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
