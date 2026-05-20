import { TestBed } from '@angular/core/testing';
import { provideZonelessChangeDetection } from '@angular/core';
import { describe, expect, it } from 'vitest';

import {
  EngineResultData,
  EngineResultsComponent,
  LeanStatistics,
} from './engine-results.component';

// ─────────────────────────────────────────────────────────────────────────
// Bug B regression coverage. The frontend's LeanStatistics interface
// expects ``{portfolio, trade, runtime}``. Sidecar runs persisted before
// the canonical-shape fix wrote a flat ``{statistics, runtime_statistics,
// parser_version, workspace_path}`` dict instead. Without the defensive
// guard, clicking such a row threw ``Cannot read properties of undefined
// (reading 'total_net_profit')`` in lean-statistics.component.ts.
// ─────────────────────────────────────────────────────────────────────────

function emptyLeanStats(): LeanStatistics {
  return {
    portfolio: {
      average_win_rate: 0, average_loss_rate: 0, profit_loss_ratio: 0,
      win_rate: 0, loss_rate: 0, expectancy: 0,
      start_equity: 100000, end_equity: 100009, total_net_profit: 0.0009,
      compounding_annual_return: 0, sharpe_ratio: 0, sortino_ratio: 0,
      probabilistic_sharpe_ratio: 0, annual_standard_deviation: 0,
      annual_variance: 0, alpha: 0, beta: 0,
      information_ratio: 0, tracking_error: 0, treynor_ratio: 0,
      drawdown: 0, drawdown_recovery: 0,
      value_at_risk_99: 0, value_at_risk_95: 0, portfolio_turnover: 0,
    },
    trade: {
      start_date_time: '', end_date_time: '',
      total_number_of_trades: 1, number_of_winning_trades: 1,
      number_of_losing_trades: 0, total_profit_loss: 9,
      total_profit: 9, total_loss: 0,
      largest_profit: 9, largest_loss: 0,
      average_profit_loss: 9, average_profit: 9, average_loss: 0,
      average_trade_duration: '0:10:00', average_winning_trade_duration: '0:10:00',
      average_losing_trade_duration: '0:00:00',
      max_consecutive_winning_trades: 1, max_consecutive_losing_trades: 0,
      profit_factor: 0, profit_to_max_drawdown_ratio: 0,
      profit_loss_standard_deviation: 0, profit_loss_downside_deviation: 0,
      sharpe_ratio: 0, sortino_ratio: 0, total_fees: 1,
    },
    runtime: {
      equity: 100009, fees: 1, net_profit: 9, total_return: 0.0009, total_orders: 1,
    },
  };
}

function baseResult(overrides: Partial<EngineResultData> = {}): EngineResultData {
  return {
    success: true,
    strategy_name: 'ema_crossover',
    fill_mode: 'open',
    initial_cash: 100000,
    final_equity: 100009,
    net_profit: 9,
    total_fees: 1,
    total_trades: 1,
    winning_trades: 1,
    losing_trades: 0,
    win_rate: 1,
    statistics: {},
    lean_statistics: null,
    trades: [],
    log_lines: [],
    ...overrides,
  };
}

function makeComponent(result: EngineResultData): EngineResultsComponent {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(EngineResultsComponent);
  fixture.componentRef.setInput('result', result);
  fixture.detectChanges();
  return fixture.componentInstance;
}

describe('EngineResultsComponent.leanStats', () => {
  it('returns the canonical shape verbatim when portfolio/trade/runtime are present', () => {
    const lean = emptyLeanStats();
    const cmp = makeComponent(baseResult({ lean_statistics: lean }));
    expect(cmp.leanStats()).toBe(lean);
  });

  it('returns null when lean_statistics is null (no dashboard rendered)', () => {
    const cmp = makeComponent(baseResult({ lean_statistics: null }));
    expect(cmp.leanStats()).toBeNull();
  });

  it('returns null for the legacy sidecar shape so history-click does not crash', () => {
    // Shape persisted by lean_sidecar_persistence before the canonical-shape fix:
    // a flat dict of LEAN's STATISTICS:: strings + workspace metadata.
    // Reading ``.portfolio.total_net_profit`` on this shape threw the
    // ``Cannot read properties of undefined`` TypeError that crashed the
    // Engine Lab.
    const legacy = {
      statistics: { 'Net Profit': '-4.657%', 'Sharpe Ratio': '-1.072' },
      runtime_statistics: { Equity: '$95,343.16' },
      parser_version: 'phase-3a-r1',
      workspace_path: '/lean-run/workspaces/xyz',
    } as unknown as LeanStatistics;
    const cmp = makeComponent(baseResult({ lean_statistics: legacy }));
    expect(cmp.leanStats()).toBeNull();
  });

  it('returns null when only some of portfolio/trade/runtime are present', () => {
    const partial = { portfolio: emptyLeanStats().portfolio } as unknown as LeanStatistics;
    const cmp = makeComponent(baseResult({ lean_statistics: partial }));
    expect(cmp.leanStats()).toBeNull();
  });
});
