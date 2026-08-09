import { TestBed } from '@angular/core/testing';
import { provideZonelessChangeDetection } from '@angular/core';
import { By } from '@angular/platform-browser';
import { describe, expect, it } from 'vitest';

import {
  EngineResultData,
  EngineResultsComponent,
  LeanStatistics,
} from './engine-results.component';
import { LeanStatisticsComponent } from '../lean-statistics/lean-statistics.component';

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
      start_date_time: 1_748_629_800_000, end_date_time: 1_748_633_400_000,
      total_number_of_trades: 1, number_of_winning_trades: 1,
      number_of_losing_trades: 0, total_profit_loss: 9,
      total_profit: 9, total_loss: 0,
      largest_profit: 9, largest_loss: 0,
      average_profit_loss: 9, average_profit: 9, average_loss: 0,
      average_trade_duration: '0:10:00', average_winning_trade_duration: '0:10:00',
      average_losing_trade_duration: '0:00:00',
      median_trade_duration: '0:10:00', median_winning_trade_duration: '0:10:00',
      median_losing_trade_duration: '0:00:00',
      max_consecutive_winning_trades: 1, max_consecutive_losing_trades: 0,
      profit_loss_ratio: 0, win_loss_ratio: 0, win_rate: 1, loss_rate: 0,
      average_mae: 0, average_mfe: 0, largest_mae: 0, largest_mfe: 0,
      maximum_closed_trade_drawdown: 0, maximum_intra_trade_drawdown: 0,
      profit_factor: 0, profit_to_max_drawdown_ratio: 0,
      profit_loss_standard_deviation: 0, profit_loss_downside_deviation: 0,
      sharpe_ratio: 0, sortino_ratio: 0, total_fees: 1,
      maximum_end_trade_drawdown: 0, average_end_trade_drawdown: 0,
      maximum_drawdown_duration: '0:00:00',
    },
    runtime: {
      equity: 100009, fees: 1, holdings: 0, net_profit: 9,
      probabilistic_sharpe_ratio: 0, total_return: 0.0009,
      unrealized: 0, volume: 1000, total_orders: 1,
    },
    namespaces: {
      status: 'match',
      source_commit: '261366a7e26ae942df858ab20df4fef8fa07de67',
      absolute_tolerance: 0.0000500001,
      native_metric_count: 66,
      formatted_metric_count: 25,
      divergences: [],
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
  it('renders every native LEAN analysis finding with its sample and solutions', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [provideZonelessChangeDetection()] });
    const fixture = TestBed.createComponent(EngineResultsComponent);
    fixture.componentRef.setInput('result', baseResult({
      lean_analysis: [
        {
          name: 'StatisticalSignificanceOfDailyReturnsAnalysis',
          issue: 'The p-value is above 0.05.',
          sample: { pValue: '0.0684907141208504' },
          solutions: ['Review the trading rules.', 'Review the universe.'],
        },
      ],
    }));
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('LEAN native analysis');
    expect(text).toContain('Statistical Significance Of Daily Returns');
    expect(text).toContain('0.0684907141208504');
    expect(text).toContain('Review the trading rules.');
    expect(text).toContain('Review the universe.');
  });

  it('returns the canonical shape verbatim when portfolio/trade/runtime are present', () => {
    const lean = emptyLeanStats();
    const cmp = makeComponent(baseResult({ lean_statistics: lean }));
    expect(cmp.leanStats()).toBe(lean);
  });

  it('renders all native portfolio, trade, and runtime fields plus the parity receipt', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [provideZonelessChangeDetection()] });
    const fixture = TestBed.createComponent(EngineResultsComponent);
    const leanStats = emptyLeanStats();
    Object.assign(leanStats.trade, {
      average_mae: -453.056,
      average_mfe: 129.5,
      largest_mae: -885.78,
      largest_mfe: 268.81,
      profit_loss_standard_deviation: 382.5295,
      profit_loss_downside_deviation: 83.0247,
    });
    fixture.componentRef.setInput('result', baseResult({ lean_statistics: leanStats }));
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('LEAN-native calculation receipt: match');
    expect(root.textContent).toContain('66 native values and 25 dashboard values');

    const nativeStats = fixture.debugElement.query(By.directive(LeanStatisticsComponent))
      .componentInstance as LeanStatisticsComponent;
    nativeStats.showFullStats.set(true);
    fixture.detectChanges();

    expect(root.querySelectorAll('.lean-stats-panel .kpi-cell')).toHaveLength(75);
    expect(root.textContent).toContain('Portfolio Turnover');
    expect(root.textContent).toContain('Median Win Duration');
    expect(root.textContent).toContain('Largest MAE');
    expect(root.textContent).toContain('Max Intra-trade Drawdown');
    expect(root.textContent).toContain('Native Runtime Snapshot');
    expect(root.textContent).toContain('Runtime PSR');
    expect(root.textContent).toContain('$-453.06');
    expect(root.textContent).toContain('$129.50');
    expect(root.textContent).toContain('$382.53');
    expect(root.textContent).toContain('$83.02');
    expect(root.textContent).not.toContain('-45305.60%');
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
