import type { DataPolicy } from "../../../models/data-policy";

export type EngineSourceLiteral = "engine" | "strategy-lab" | "lean-sidecar";

/** PR B (2026-05-19) — the unified, backend-neutral engine identity used by
 *  the history table's filter dropdown and Engine column. */
export type EngineLiteral = "PYTHON" | "LEAN";

export interface RunHistoryRow {
  /** StrategyExecution.Id as a string (GraphQL ID type). */
  id: string;
  /** Engine that produced this run. */
  source: EngineSourceLiteral;
  /** PR B (2026-05-19) — unified engine identity, derived from ``source``
   *  server-side. Drives the Engine column + filter dropdown. */
  engine: EngineLiteral;
  /** Strategy or template name, e.g. "ema_crossover". */
  strategyName: string;
  /** Symbol if known (parsed from Parameters JSON or stored separately). */
  symbol: string | null;
  /** ISO date string for display, e.g. "2025-01-06". */
  startDate: string;
  endDate: string;
  /** int64 ms UTC from StrategyExecution.ExecutedAt. */
  executedAt: number;
  totalTrades: number;
  totalPnl: number;
  /** True if any of the run's trades are synthetic MTM exits. */
  hasSyntheticExit: boolean;
  /** LEAN run id; null for engine-source rows. */
  leanRunId: string | null;
  /** PR B — canonical DataPolicy block for the Bars summary column.
   *  Null on legacy rows (predating the DataPolicyJson column). */
  dataPolicy: DataPolicy | null;
  /** PR B — free-text researcher notes, editable inline. */
  notes: string | null;
  /** PR B — commission per order. Surfaced as a tooltip / detail column. */
  commissionPerOrder: number | null;
  /** PR B — brokerage policy ("algorithm_default" / IB / etc.). */
  brokeragePolicy: string | null;
}
