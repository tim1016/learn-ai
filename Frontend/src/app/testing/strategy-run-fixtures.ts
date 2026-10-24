import type { RunLedger, StrategyRunResponse } from '../services/strategy-runs.types';

/**
 * One persisted spec-run ledger, shaped as the run ledger stores it. Its spec
 * id reads like a registered EMA strategy on purpose: a free-form spec run is
 * identified by its own run id and spec hash, never by a name that resembles a
 * registry key.
 */
export function makeRunLedger(overrides: Partial<RunLedger> = {}): RunLedger {
  return {
    schema_version: '1.0',
    run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    parent_run_id: null,
    parent_spec_hash: null,
    strategy_spec_id: 'spy_ema_crossover',
    strategy_spec_hash: 'd'.repeat(64),
    strategy_spec_json: { name: 'spy_ema_crossover' },
    engine_name: 'learn_ai_event_driven',
    engine_version: '0.1.0',
    engine_git_commit: 'unknown',
    symbol: 'SPY',
    resolution_minutes: 15,
    // The window's ends as the engine stores them: ET midnight of 2024-01-02 and of 2024-12-31.
    start_ms: 1704171600000,
    end_ms: 1735621200000,
    initial_cash: 100000,
    fill_mode: 'signal_bar_close',
    commission_per_order: 0,
    slippage_per_share: 0,
    warmup_policy: 'spec_indicator_warmup',
    random_seed: 0,
    data_source: 'lean_minute_reader',
    data_snapshot_id: 'SPY|15|...|test',
    result_hash: 'r'.repeat(64),
    trade_log_hash: 't'.repeat(64),
    metrics_hash: 'm'.repeat(64),
    created_at_ms: 1736000000000,
    completed_at_ms: 1736000001000,
    status: 'completed',
    failure_reason: null,
    ...overrides,
  };
}

/** A completed run with no trades: the ledger and a result that agree on the run id. */
export function makeRunResponse(ledger: RunLedger = makeRunLedger()): StrategyRunResponse {
  return {
    ledger,
    result: {
      run_id: ledger.run_id,
      initial_cash: 100000,
      final_equity: 102500,
      equity_curve: [],
      drawdown_curve: [],
      trades: [],
      metrics: {
        total_trades: 0,
        winning_trades: 0,
        losing_trades: 0,
        win_rate: null,
        total_return_pct: 0.025,
        max_drawdown_pct: 0,
        sharpe_ratio: null,
        sortino_ratio: null,
        profit_factor: null,
        expectancy_pct: null,
        payoff_ratio: null,
        exposure_pct: null,
        avg_trade_bars: null,
      },
      log_lines: [],
      warnings: [],
    },
  };
}
