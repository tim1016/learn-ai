export type EngineSourceLiteral = "engine" | "strategy-lab" | "lean-sidecar";

export interface RunHistoryRow {
  /** StrategyExecution.Id as a string (GraphQL ID type). */
  id: string;
  /** Engine that produced this run. */
  source: EngineSourceLiteral;
  /** Strategy or template name, e.g. "ema_crossover". */
  strategyName: string;
  /** Symbol if known (parsed from Parameters JSON or stored separately). */
  symbol: string | null;
  /** ISO date string for display, e.g. "2025-01-06". */
  startDate: string;
  endDate: string;
  /** ISO timestamp string from StrategyExecution.ExecutedAt. */
  executedAt: string;
  totalTrades: number;
  totalPnl: number;
  /** True if any of the run's trades are synthetic MTM exits. */
  hasSyntheticExit: boolean;
  /** LEAN run id; null for engine-source rows. */
  leanRunId: string | null;
}
