/**
 * Backtest runs as the Python service serves them (PRD #1929).
 *
 * Top-level field names are the ones the retired GraphQL `backtestRuns` /
 * `backtestRun` queries emitted — the run-history table and the run report
 * read exactly these. The heavy envelopes (equity curve, validation
 * analytics, data policy, metric documentation) arrive in the producer's
 * own stored shape: the Python builders are their single source of truth,
 * so nothing re-types them on the way here. Every temporal value is
 * `int64 ms UTC`; `startDate` / `endDate` are date-anchored `YYYY-MM-DD`.
 */

import type { EngineValidationAnalytics } from "../components/lean-engine/engine-results/engine-validation-analytics.types";
import type { RunHistoryRow } from "../components/shared/run-history/run-history.types";
import type { DataPolicy } from "../models/data-policy";

/** Unified engine identity used by the history filter and the Engine column. */
export type Engine = "PYTHON" | "LEAN";

export type RunSource = "engine" | "lean-sidecar";

/** One run-history row. */
export interface BacktestRunSummary {
  id: number;
  source: RunSource;
  engine: Engine;
  strategyName: string;
  /** Stored on the run row; no longer parsed out of the parameters JSON. */
  symbol: string;
  leanRunId: string | null;
  /** The run's configuration as a JSON string. */
  parameters: string;
  startDate: string;
  endDate: string;
  executedAt: number;
  totalTrades: number;
  totalPnL: number;
  commissionPerOrder: number | null;
  brokeragePolicy: string | null;
  /** Free-text researcher notes, editable inline. */
  notes: string | null;
  dataPolicy: DataPolicy | null;
  verdictGrade: string | null;
  verdictSignal: string | null;
  parityGroupId: string | null;
  hasSyntheticExit: boolean;
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

/** One curve of the producer's strict dual-curve equity report. */
export interface BacktestRunEquitySeries {
  cadence: string | null;
  downsample?: { raw_points: number; kept_points: number; policy?: string };
  points: { t: number; e: number }[];
  error?: string | null;
}

/** `build_run_equity_envelope` as stored — schema version 2. */
export interface BacktestRunEquityEnvelope {
  schema_version: number;
  error?: string | null;
  mark_to_market: BacktestRunEquitySeries | null;
  realized: BacktestRunEquitySeries | null;
}

/** `build_validation_analytics_envelope` as stored; the analytics body is the canonical snake_case shape. */
export interface BacktestRunValidationAnalyticsEnvelope {
  schema_version: number;
  computed_at_ms: number;
  engine: string;
  analytics: EngineValidationAnalytics;
}

/** One recorded metric-documentation context, as the producer catalogue writes it. */
export interface MetricDocumentationContext {
  metric_id: string;
  variant_id: string;
  /** Python-authored provenance token; the client only transports and labels it. */
  producer: string;
  contract_id: string | null;
}

export interface BacktestRunParityVerdict {
  id: number;
  status: string;
  verdictJson: string;
  createdAt: number;
}

export interface BacktestRunDetail {
  id: number;
  engine: Engine;
  source: RunSource;
  requestedEngine: "python" | "lean" | "both" | null;
  strategyName: string;
  symbol: string;
  leanRunId: string | null;
  parameters: string;
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
  commissionPerOrder: number | null;
  finalEquity: number;
  totalFees: number;
  maxDrawdown: number;
  sharpeRatio: number | null;
  sortinoRatio: number | null;
  profitFactor: number | null;
  leanStatisticsJson: string | null;
  /** Complete native LEAN analysis catalog, preserved as JSON. */
  leanAnalysisJson: string | null;
  verdictJson: string | null;
  verdictVersion: number | null;
  verdictGrade: string | null;
  verdictSignal: string | null;
  equityCurve: BacktestRunEquityEnvelope | null;
  validationAnalytics: BacktestRunValidationAnalyticsEnvelope | null;
  metricDocumentation: MetricDocumentationContext[];
  /** The run report's key for re-fetching chart bars. */
  dataPolicy: DataPolicy | null;
  insightSummaryJson: string | null;
  parityGroupId: string | null;
  notes: string | null;
  trades: BacktestRunDetailTrade[];
  /** True when the response carries only the newest bounded trade evidence. */
  tradesTruncated: boolean;
  parityVerdicts: BacktestRunParityVerdict[];
}

export function toRunHistoryRow(run: BacktestRunSummary): RunHistoryRow {
  return {
    id: String(run.id),
    source: run.source,
    engine: run.engine,
    strategyName: run.strategyName,
    symbol: run.symbol,
    startDate: run.startDate,
    endDate: run.endDate,
    executedAt: run.executedAt,
    totalTrades: run.totalTrades,
    totalPnl: run.totalPnL,
    hasSyntheticExit: run.hasSyntheticExit,
    leanRunId: run.leanRunId,
    dataPolicy: run.dataPolicy,
    notes: run.notes,
    commissionPerOrder: run.commissionPerOrder,
    brokeragePolicy: run.brokeragePolicy,
    verdictGrade: run.verdictGrade,
    verdictSignal: run.verdictSignal,
    parityGroupId: run.parityGroupId,
  };
}
