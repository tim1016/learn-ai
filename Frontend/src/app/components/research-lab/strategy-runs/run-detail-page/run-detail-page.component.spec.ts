import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RunDetailPageComponent } from './run-detail-page.component';
import { StrategyRunsService } from '../../../../services/strategy-runs.service';
import type { StrategyRunResponse } from '../../../../services/strategy-runs.types';

function makeRunResponse(): StrategyRunResponse {
  return {
    ledger: {
      schema_version: '1.0',
      run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      parent_run_id: null,
      parent_spec_hash: null,
      strategy_spec_id: 'spy_ema_crossover',
      strategy_spec_hash: 'd'.repeat(64),
      strategy_spec_json: { name: 'spy_ema_crossover' },
      engine_name: 'learn_ai_event_driven',
      engine_version: '0.1.0',
      engine_git_commit: 'abc1234567890def',
      symbol: 'SPY',
      resolution_minutes: 15,
      start_ms: 1704160800000,
      end_ms: 1735714800000,
      initial_cash: 100000,
      fill_mode: 'signal_bar_close',
      commission_per_order: 0,
      slippage_per_share: 0,
      warmup_policy: 'spec_indicator_warmup',
      random_seed: 0,
      data_source: 'lean_minute_reader',
      data_snapshot_id: 'SPY|15|1704160800000|1735714800000|abc',
      result_hash: 'r'.repeat(64),
      trade_log_hash: 't'.repeat(64),
      metrics_hash: 'm'.repeat(64),
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
    result: {
      run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      initial_cash: 100000,
      final_equity: 105432.10,
      equity_curve: [
        { timestamp_ms: 1704160800000, equity: 100000 },
        { timestamp_ms: 1704161700000, equity: 100250 },
      ],
      drawdown_curve: [
        { timestamp_ms: 1704160800000, drawdown_pct: 0 },
        { timestamp_ms: 1704161700000, drawdown_pct: 0.012 },
      ],
      trades: [
        {
          trade_number: 1,
          entry_time_ms: 1704160800000,
          entry_price: 470.5,
          exit_time_ms: 1704165300000,
          exit_price: 472.1,
          indicators_at_entry: { ema5: 470.4, ema10: 470.0, rsi14: 58.0 },
          pnl_pts: 1.6,
          pnl_pct: 0.0034,
          result: 'WIN',
          signal_reason: 'ema cross + rsi gate',
          bars_held: 5,
        },
      ],
      metrics: {
        total_trades: 1,
        winning_trades: 1,
        losing_trades: 0,
        win_rate: 1,
        total_return_pct: 0.0543,
        max_drawdown_pct: 0.012,
        sharpe_ratio: 1.85,
        sortino_ratio: 2.31,
        profit_factor: null,
        expectancy_pct: 0.0034,
        payoff_ratio: null,
        exposure_pct: 0.04,
        avg_trade_bars: 5,
      },
      log_lines: [],
      warnings: [],
    },
  };
}

interface ServiceMock {
  getRun: ReturnType<typeof vi.fn>;
  listRuns: ReturnType<typeof vi.fn>;
  createRun: ReturnType<typeof vi.fn>;
  runSpyEmaFixture: ReturnType<typeof vi.fn>;
}

describe('RunDetailPageComponent', () => {
  let component: RunDetailPageComponent;
  let fixture: ComponentFixture<RunDetailPageComponent>;
  let service: ServiceMock;

  beforeEach(async () => {
    service = {
      getRun: vi.fn().mockResolvedValue(makeRunResponse()),
      listRuns: vi.fn(),
      createRun: vi.fn(),
      runSpyEmaFixture: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [RunDetailPageComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: StrategyRunsService, useValue: service },
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: of(
              convertToParamMap({ run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }),
            ),
          },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(RunDetailPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('renders a loaded run: route run_id fetched, spec id header, server-computed metrics, trade table, truncated provenance hashes', () => {
    expect
      .soft(service.getRun, 'reads the run_id from the route and fetches the run: fetch call')
      .toHaveBeenCalledWith('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
    expect
      .soft(component.runId(), 'reads the run_id from the route and fetches the run: run id')
      .toBe('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
    expect
      .soft(component.run()?.ledger.symbol, 'reads the run_id from the route and fetches the run: loaded run symbol')
      .toBe('SPY');

    const heading = fixture.nativeElement.querySelector('.rd-title h1');
    expect.soft(heading?.textContent, 'renders the spec id in the header').toContain('spy_ema_crossover');

    const labels = Array.from(
      fixture.nativeElement.querySelectorAll('.rd-metric-label'),
    ).map((n) => (n as HTMLElement).textContent?.trim());
    // Sample of labels — order/exact set may evolve, but these must be present.
    expect
      .soft(labels, 'renders metric values from the server payload (no client-side computation): metric labels')
      .toEqual(expect.arrayContaining(['Total return', 'Max drawdown', 'Sharpe', 'Trades']));

    const text: string = fixture.nativeElement.textContent ?? '';
    // 0.0543 → 5.43% via PercentPipe '1.2-2'
    expect
      .soft(text, 'renders metric values from the server payload (no client-side computation): total return as a percent')
      .toContain('5.43%');
    // 1.85 sharpe via DecimalPipe '1.2-2'
    expect
      .soft(text, 'renders metric values from the server payload (no client-side computation): sharpe')
      .toContain('1.85');

    const rows = fixture.nativeElement.querySelectorAll('.rd-trade-table tbody tr');
    expect.soft(rows.length, 'renders the trade table with one row per trade: row count').toBe(1);
    const cells = Array.from(rows[0].querySelectorAll('td')).map(
      (n) => (n as HTMLElement).textContent?.trim() ?? '',
    );
    // First cell is trade_number, last is result tag.
    expect.soft(cells[0], 'renders the trade table with one row per trade: first cell is trade_number').toBe('1');
    expect
      .soft(cells[cells.length - 1], 'renders the trade table with one row per trade: last cell is the result tag')
      .toContain('WIN');

    expect
      .soft(text, 'renders provenance hashes truncated to 32 chars + ellipsis: spec hash')
      .toContain('d'.repeat(32) + '…');
    expect
      .soft(text, 'renders provenance hashes truncated to 32 chars + ellipsis: result hash')
      .toContain('r'.repeat(32) + '…');
    expect
      .soft(text, 'renders provenance hashes truncated to 32 chars + ellipsis: data snapshot id')
      .toContain('SPY|15|1704160800000|1735714800000|abc');
  });

  it('surfaces service errors', async () => {
    service.getRun.mockRejectedValueOnce(new Error('fetch failed'));
    await component.load('b'.repeat(32));
    fixture.detectChanges();
    expect(component.error()).toBe('fetch failed');
  });
});
