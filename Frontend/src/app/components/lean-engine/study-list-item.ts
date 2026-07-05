/**
 * Shape of a row returned by ``GET /api/studies`` (.NET StudiesApi REST
 * endpoint) and synthesized from a just-finished backtest for the Replay
 * tab. Lifted out of the now-deleted ``EngineHistoryComponent`` (PR B.3
 * Task 3.6) so downstream consumers (Replay, the study-selected handler)
 * keep their type contract without depending on a component that has
 * been removed.
 *
 * The REST endpoint itself stays alive — replay / inspection flows still
 * use it. Only the table component that consumed it via list+sort+paginate
 * was retired in favor of the unified GraphQL-backed history surface.
 */
export interface StudyListItem {
  id: number;
  symbol: string;
  strategyName: string;
  startDate: string;
  endDate: string;
  timespan: string;
  fillMode: string;
  source: string;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  totalPnL: number;
  maxDrawdown: number;
  sharpeRatio: number;
  sortinoRatio: number;
  compoundingAnnualReturn: number;
  probabilisticSharpeRatio: number;
  profitFactor: number;
  valueAtRisk95: number;
  alpha: number;
  beta: number;
  initialCash: number;
  finalEquity: number;
  parameters: string;
  notes: string | null;
  executedAt: number;
  durationMs: number;
}
