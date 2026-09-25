/**
 * Shared wire types for engine results (#2447): the former
 * ``EngineResultsComponent`` served double duty as this barrel. The component
 * itself was dead UI -- Strategy Lab renders the run report through
 * ``app-strategy-lab-run-stats`` -- so only the types survive, under an
 * honest name.
 */

import type { EngineValidationAnalytics } from './engine-validation-analytics.types';

// ── Shared types (mirrored from lean-engine) ──────────────────
export interface LeanPortfolioStats {
  average_win_rate: number; average_loss_rate: number; profit_loss_ratio: number;
  win_rate: number; loss_rate: number; expectancy: number;
  start_equity: number; end_equity: number; total_net_profit: number;
  compounding_annual_return: number; sharpe_ratio: number; sortino_ratio: number;
  probabilistic_sharpe_ratio: number; annual_standard_deviation: number;
  annual_variance: number; alpha: number; beta: number;
  information_ratio: number; tracking_error: number; treynor_ratio: number;
  drawdown: number; drawdown_recovery: number;
  value_at_risk_99: number; value_at_risk_95: number; portfolio_turnover: number;
}

export interface LeanTradeStats {
  start_date_time: number | null; end_date_time: number | null;
  total_number_of_trades: number; number_of_winning_trades: number;
  number_of_losing_trades: number; total_profit_loss: number;
  total_profit: number; total_loss: number;
  largest_profit: number; largest_loss: number;
  average_profit_loss: number; average_profit: number; average_loss: number;
  average_trade_duration: string; average_winning_trade_duration: string;
  average_losing_trade_duration: string;
  median_trade_duration: string; median_winning_trade_duration: string;
  median_losing_trade_duration: string;
  max_consecutive_winning_trades: number; max_consecutive_losing_trades: number;
  profit_loss_ratio: number; win_loss_ratio: number; win_rate: number; loss_rate: number;
  average_mae: number; average_mfe: number; largest_mae: number; largest_mfe: number;
  maximum_closed_trade_drawdown: number; maximum_intra_trade_drawdown: number;
  profit_factor: number; profit_to_max_drawdown_ratio: number;
  profit_loss_standard_deviation: number; profit_loss_downside_deviation: number;
  sharpe_ratio: number; sortino_ratio: number; total_fees: number;
  maximum_end_trade_drawdown: number; average_end_trade_drawdown: number;
  maximum_drawdown_duration: string;
}

export interface LeanRuntimeStats {
  equity: number; fees: number; holdings: number; net_profit: number;
  probabilistic_sharpe_ratio: number; total_return: number;
  unrealized: number; volume: number; total_orders: number;
}

export interface LeanNativeMetricParityReceipt {
  status: 'match' | 'mismatch' | 'unavailable';
  reason?: string | null;
  contract_id?: string;
  source_commit?: string;
  absolute_tolerance?: number;
  native_metric_count?: number;
  formatted_metric_count?: number;
  divergences?: unknown[];
}

export interface LeanStatistics {
  portfolio: LeanPortfolioStats;
  trade: LeanTradeStats;
  runtime: LeanRuntimeStats;
  namespaces?: LeanNativeMetricParityReceipt;
}

export interface LeanAnalysisFinding {
  name: string;
  issue: string;
  sample: unknown;
  solutions: string[];
  [key: string]: unknown;
}

export interface EngineTrade {
  trade_number: number;
  entry_time: number;
  entry_price: number;
  exit_time: number;
  exit_price: number;
  quantity?: number;
  indicators: Record<string, number>;
  pnl_pts: number;
  pnl_pct: number;
  result: string;
  signal_reason: string;
}

export interface EngineResultData {
  success: boolean;
  strategy_name: string;
  fill_mode: string;
  initial_cash: number;
  final_equity: number;
  net_profit: number;
  total_fees: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  statistics: Record<string, number | null>;
  lean_statistics: LeanStatistics | null;
  lean_analysis?: LeanAnalysisFinding[];
  trades: EngineTrade[];
  log_lines: string[];
  validation_analytics?: EngineValidationAnalytics | null;
  error?: string;
}
