import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MonteCarloService } from '../../../../../services/monte-carlo.service';
import type {
  MonteCarloConfig,
  MonteCarloListResponse,
  MonteCarloResponse,
} from '../../../../../services/monte-carlo.types';
import type { RunLedger } from '../../../../../services/strategy-runs.types';
import { MonteCarloSectionComponent } from './monte-carlo-section.component';

function makeRunLedger(overrides: Partial<RunLedger> = {}): RunLedger {
  return {
    schema_version: '1.0',
    run_id: 'a'.repeat(32),
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

function makeMcConfig(overrides: Partial<MonteCarloConfig> = {}): MonteCarloConfig {
  return {
    monte_carlo_id: 'b'.repeat(32),
    parent_run_id: 'a'.repeat(32),
    parent_trade_log_hash: 't'.repeat(64),
    method: 'reshuffle',
    simulation_count: 1000,
    projection_trade_count: 0,
    initial_equity: 100000,
    random_seed: 0,
    breach_thresholds: [0.05, 0.1],
    created_at_ms: 1736000000000,
    ...overrides,
  };
}

function makeMcResponse(): MonteCarloResponse {
  return {
    config: makeMcConfig(),
    result: {
      monte_carlo_id: 'b'.repeat(32),
      parent_run_id: 'a'.repeat(32),
      method: 'reshuffle',
      simulation_count: 1000,
      realised_trade_count: 25,
      equity_bands: [],
      drawdown_quantiles: { p5: 0.01, p50: 0.05, p95: 0.12 },
      terminal_pnl_quantiles: { p5: -100, p50: 500, p95: 1500 },
      max_losing_streak_quantiles: { p5: 1, p50: 2, p95: 4 },
      breach_probabilities: [],
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

interface ServiceMock {
  listMonteCarlos: ReturnType<typeof vi.fn>;
  runReshuffleFromRun: ReturnType<typeof vi.fn>;
  createMonteCarlo: ReturnType<typeof vi.fn>;
  getMonteCarlo: ReturnType<typeof vi.fn>;
}

describe('MonteCarloSectionComponent', () => {
  let fixture: ComponentFixture<MonteCarloSectionComponent>;
  let component: MonteCarloSectionComponent;
  let service: ServiceMock;
  let routerNavigate: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    const empty: MonteCarloListResponse = { monte_carlos: [] };
    service = {
      listMonteCarlos: vi.fn().mockResolvedValue(empty),
      runReshuffleFromRun: vi.fn().mockResolvedValue(makeMcResponse()),
      createMonteCarlo: vi.fn(),
      getMonteCarlo: vi.fn(),
    };
    routerNavigate = vi.fn().mockResolvedValue(true);

    await TestBed.configureTestingModule({
      imports: [MonteCarloSectionComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: MonteCarloService, useValue: service },
        { provide: Router, useValue: { navigate: routerNavigate } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(MonteCarloSectionComponent);
    fixture.componentRef.setInput('run', makeRunLedger());
    component = fixture.componentInstance;
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads MCs filtered by parent_run_id on input arrival', () => {
    expect(service.listMonteCarlos).toHaveBeenCalledWith({
      parent_run_id: 'a'.repeat(32),
      limit: 50,
    });
  });

  it('shows empty-state copy when no MCs exist', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('No Monte Carlos yet');
  });

  it('runReshuffle calls runReshuffleFromRun, refreshes the list, and remembers the new mc_id', async () => {
    service.listMonteCarlos.mockClear();
    await component.runReshuffle();
    expect(service.runReshuffleFromRun).toHaveBeenCalledOnce();
    expect(service.listMonteCarlos).toHaveBeenCalledOnce();
    expect(component.lastMcId()).toBe('b'.repeat(32));
  });

  it('open(mc) navigates to the MC detail route', () => {
    component.open(makeMcConfig({ monte_carlo_id: 'c'.repeat(32) }));
    expect(routerNavigate).toHaveBeenCalledWith([
      '/research-lab/monte-carlo',
      'c'.repeat(32),
    ]);
  });

  it('renders existing MCs in the table', async () => {
    service.listMonteCarlos.mockResolvedValueOnce({
      monte_carlos: [
        makeMcConfig({
          monte_carlo_id: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
          method: 'resample',
          simulation_count: 500,
          random_seed: 42,
        }),
      ],
    });
    await component.refresh();
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('eeeeeeeeeeeeeeee');
    expect(text).toContain('resample');
    expect(text).toContain('500');
    expect(text).toContain('42');
  });

  it('surfaces service errors', async () => {
    service.listMonteCarlos.mockRejectedValueOnce(new Error('network down'));
    await component.refresh();
    expect(component.error()).toBe('network down');
  });
});
