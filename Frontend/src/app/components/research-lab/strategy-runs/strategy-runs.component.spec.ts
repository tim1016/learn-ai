import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { StrategyRunsComponent } from './strategy-runs.component';
import { StrategyRunsService } from '../../../services/strategy-runs.service';
import type {
  RunLedger,
  StrategyRunResponse,
} from '../../../services/strategy-runs.types';

function makeLedger(overrides: Partial<RunLedger> = {}): RunLedger {
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
    start_ms: 1704160800000,
    end_ms: 1735714800000,
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

function makeRunResponse(): StrategyRunResponse {
  return {
    ledger: makeLedger(),
    result: {
      run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
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

interface ServiceMock {
  listRuns: ReturnType<typeof vi.fn>;
  runSpyEmaFixture: ReturnType<typeof vi.fn>;
  getRun: ReturnType<typeof vi.fn>;
  createRun: ReturnType<typeof vi.fn>;
}

describe('StrategyRunsComponent', () => {
  let component: StrategyRunsComponent;
  let fixture: ComponentFixture<StrategyRunsComponent>;
  let service: ServiceMock;
  let routerNavigate: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    service = {
      listRuns: vi.fn().mockResolvedValue({ runs: [makeLedger()] }),
      runSpyEmaFixture: vi.fn().mockResolvedValue(makeRunResponse()),
      getRun: vi.fn(),
      createRun: vi.fn(),
    };
    routerNavigate = vi.fn().mockResolvedValue(true);

    await TestBed.configureTestingModule({
      imports: [StrategyRunsComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: StrategyRunsService, useValue: service },
        { provide: Router, useValue: { navigate: routerNavigate } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(StrategyRunsComponent);
    component = fixture.componentInstance;
    // Wait for the constructor's initial refresh() to settle.
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads runs from the service on construction', () => {
    expect(service.listRuns).toHaveBeenCalledOnce();
    expect(component.runs()).toHaveLength(1);
    expect(component.runs()[0]?.symbol).toBe('SPY');
  });

  it('refresh() re-issues the listRuns call', async () => {
    service.listRuns.mockClear();
    service.listRuns.mockResolvedValueOnce({ runs: [] });
    await component.refresh();
    expect(service.listRuns).toHaveBeenCalledOnce();
    expect(component.runs()).toEqual([]);
  });

  it('runFixture() POSTs the fixture, then refreshes, and remembers the new run_id', async () => {
    service.listRuns.mockClear();
    await component.runFixture();
    expect(service.runSpyEmaFixture).toHaveBeenCalledOnce();
    expect(service.listRuns).toHaveBeenCalledOnce();
    expect(component.lastFixtureRunId()).toBe(
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    );
  });

  it('open(run) navigates to the detail route', () => {
    const ledger = makeLedger({ run_id: 'b'.repeat(32) });
    component.open(ledger);
    expect(routerNavigate).toHaveBeenCalledWith([
      '/research-lab/strategy-runs',
      'b'.repeat(32),
    ]);
  });

  it('surfaces service errors without throwing', async () => {
    service.listRuns.mockRejectedValueOnce(new Error('boom'));
    await component.refresh();
    expect(component.error()).toBe('boom');
    expect(component.runs()).toHaveLength(1); // last successful state preserved
  });

  describe('statusSeverity', () => {
    it('maps every status to a severity token', () => {
      expect(component.statusSeverity('completed')).toBe('success');
      expect(component.statusSeverity('running')).toBe('info');
      expect(component.statusSeverity('failed')).toBe('danger');
    });
  });

  describe('shortHash', () => {
    it('returns "—" for nullish input', () => {
      expect(component.shortHash(null)).toBe('—');
      expect(component.shortHash('')).toBe('—');
    });

    it('truncates to the requested length', () => {
      expect(component.shortHash('abcdefghij', 4)).toBe('abcd');
    });
  });
});
