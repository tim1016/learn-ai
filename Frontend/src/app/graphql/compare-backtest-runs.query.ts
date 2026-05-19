import { gql } from "apollo-angular";

export const COMPARE_BACKTEST_RUNS_QUERY = gql`
  query CompareBacktestRuns($leftId: Int!, $rightId: Int!) {
    compareBacktestRuns(leftId: $leftId, rightId: $rightId) {
      left {
        id
        source
        strategyName
        leanRunId
        totalTrades
        totalPnL
        finalEquity
        trades {
          entryTimestamp
          exitTimestamp
          entryPrice
          exitPrice
          pnL
          isSyntheticExit
          signalReason
        }
      }
      right {
        id
        source
        strategyName
        leanRunId
        totalTrades
        totalPnL
        finalEquity
        trades {
          entryTimestamp
          exitTimestamp
          entryPrice
          exitPrice
          pnL
          isSyntheticExit
          signalReason
        }
      }
      guardrails {
        sameAlgorithm
        sameSymbol
        sameWindow
        sameParameters
        warnings
      }
      summary {
        pnlDelta
        tradeCountDelta
        winRateDelta
        feesDelta
        finalEquityDelta
      }
      divergences {
        category
        tradeNumber
        msUtc
        message
        leftFillPrice
        rightFillPrice
      }
      firstDivergenceMsUtc
    }
  }
`;

export type DivergenceCategory =
  | "DECISION_MISMATCH"
  | "DIRECTION_MISMATCH"
  | "QUANTITY_MISMATCH"
  | "FILL_PRICE_DRIFT"
  | "COMMISSION_DRIFT"
  | "PNL_DRIFT"
  | "ORDER_TYPE_MISMATCH"
  | "FIXTURE_INSUFFICIENT";

export interface ComparableTrade {
  entryTimestamp: string;
  exitTimestamp: string;
  entryPrice: number;
  exitPrice: number;
  pnL: number;
  isSyntheticExit: boolean;
  signalReason: string;
}

export interface ComparedRun {
  id: number;
  source: string;
  strategyName: string;
  leanRunId: string | null;
  totalTrades: number;
  totalPnL: number;
  finalEquity: number;
  trades: ComparableTrade[];
}

export interface ComparisonGuardrails {
  sameAlgorithm: boolean;
  sameSymbol: boolean;
  sameWindow: boolean;
  sameParameters: boolean;
  warnings: string[];
}

export interface ComparisonSummary {
  pnlDelta: number;
  tradeCountDelta: number;
  winRateDelta: number;
  feesDelta: number;
  finalEquityDelta: number;
}

export interface TradeDivergence {
  category: DivergenceCategory;
  tradeNumber: number | null;
  msUtc: number | null;
  message: string;
  leftFillPrice: number | null;
  rightFillPrice: number | null;
}

export interface RunComparisonResult {
  left: ComparedRun;
  right: ComparedRun;
  guardrails: ComparisonGuardrails;
  summary: ComparisonSummary;
  divergences: TradeDivergence[];
  firstDivergenceMsUtc: number | null;
}

export interface CompareBacktestRunsQueryResult {
  compareBacktestRuns: RunComparisonResult | null;
}

export interface CompareBacktestRunsQueryVariables {
  leftId: number;
  rightId: number;
}
