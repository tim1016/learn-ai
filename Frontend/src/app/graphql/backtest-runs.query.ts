import { gql } from "apollo-angular";

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
