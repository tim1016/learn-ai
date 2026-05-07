import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { RunLedger } from '../../../../../services/strategy-runs.types';
import { WalkForwardService } from '../../../../../services/walk-forward.service';
import type {
  WalkForwardConfig,
  WalkForwardListResponse,
  WalkForwardResponse,
} from '../../../../../services/walk-forward.types';
import { WalkForwardSectionComponent } from './walk-forward-section.component';

function makeRunLedger(overrides: Partial<RunLedger> = {}): RunLedger {
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

function makeWfConfig(overrides: Partial<WalkForwardConfig> = {}): WalkForwardConfig {
  return {
    walk_forward_id: 'b'.repeat(32),
    parent_run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    strategy_spec_hash: 'd'.repeat(64),
    strategy_spec_json: { name: 'test' },
    symbol: 'SPY',
    resolution_minutes: 15,
    start_ms: 1704171600000,
    end_ms: 1735621200000,
    initial_cash: 100000,
    fill_mode: 'signal_bar_close',
    commission_per_order: 0,
    slippage_per_share: 0,
    random_seed: 0,
    split_policy: { kind: 'rolling', train_days: 60, test_days: 30, step_days: 30 },
    created_at_ms: 1736000000000,
    ...overrides,
  };
}

function makeWfResponse(): WalkForwardResponse {
  return {
    config: makeWfConfig(),
    result: {
      walk_forward_id: 'b'.repeat(32),
      parent_run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      strategy_spec_hash: 'd'.repeat(64),
      split_policy: { kind: 'rolling', train_days: 60, test_days: 30, step_days: 30 },
      folds: [],
      combined_oos_equity_curve: [],
      mean_oos_sharpe: null,
      median_oos_sharpe: null,
      pct_profitable_folds: null,
      oos_retention: null,
      alpha_decay: null,
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

interface ServiceMock {
  listWalkForwards: ReturnType<typeof vi.fn>;
  runFromRun: ReturnType<typeof vi.fn>;
  createWalkForward: ReturnType<typeof vi.fn>;
  getWalkForward: ReturnType<typeof vi.fn>;
}

describe('WalkForwardSectionComponent', () => {
  let fixture: ComponentFixture<WalkForwardSectionComponent>;
  let component: WalkForwardSectionComponent;
  let service: ServiceMock;
  let routerNavigate: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    const empty: WalkForwardListResponse = { walk_forwards: [] };
    service = {
      listWalkForwards: vi.fn().mockResolvedValue(empty),
      runFromRun: vi.fn().mockResolvedValue(makeWfResponse()),
      createWalkForward: vi.fn(),
      getWalkForward: vi.fn(),
    };
    routerNavigate = vi.fn().mockResolvedValue(true);

    await TestBed.configureTestingModule({
      imports: [WalkForwardSectionComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: WalkForwardService, useValue: service },
        { provide: Router, useValue: { navigate: routerNavigate } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(WalkForwardSectionComponent);
    fixture.componentRef.setInput('run', makeRunLedger());
    component = fixture.componentInstance;
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads walk-forwards filtered by parent_run_id on input arrival', () => {
    expect(service.listWalkForwards).toHaveBeenCalledWith({
      parent_run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      limit: 50,
    });
  });

  it('shows empty-state copy when no walk-forwards exist', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('No walk-forwards yet');
  });

  it('runRollingWalkForward calls runFromRun, refreshes the list, and remembers the new wf_id', async () => {
    service.listWalkForwards.mockClear();
    await component.runRollingWalkForward();
    expect(service.runFromRun).toHaveBeenCalledOnce();
    // Refresh fires after the create completes.
    expect(service.listWalkForwards).toHaveBeenCalledOnce();
    expect(component.lastWfId()).toBe('b'.repeat(32));
  });

  it('open(wf) navigates to the WF detail route', () => {
    component.open(makeWfConfig({ walk_forward_id: 'c'.repeat(32) }));
    expect(routerNavigate).toHaveBeenCalledWith([
      '/research-lab/walk-forward',
      'c'.repeat(32),
    ]);
  });

  it('renders existing walk-forwards in the table', async () => {
    service.listWalkForwards.mockResolvedValueOnce({
      walk_forwards: [
        makeWfConfig({
          walk_forward_id: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
          split_policy: {
            kind: 'rolling',
            train_days: 60,
            test_days: 30,
            step_days: 30,
          },
        }),
      ],
    });
    await component.refresh();
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('eeeeeeeeeeeeeeee');
    expect(text).toContain('rolling');
    expect(text).toContain('60d train');
  });

  it('surfaces service errors', async () => {
    service.listWalkForwards.mockRejectedValueOnce(new Error('network down'));
    await component.refresh();
    expect(component.error()).toBe('network down');
  });

  describe('splitSummary', () => {
    it('formats chronological with the train_pct', () => {
      expect(
        component.splitSummary({ kind: 'chronological', train_pct: 0.7 }),
      ).toContain('70%');
    });

    it('formats rolling with train/test/step', () => {
      expect(
        component.splitSummary({
          kind: 'rolling',
          train_days: 60,
          test_days: 30,
          step_days: 30,
        }),
      ).toContain('60d train');
    });

    it('formats anchored', () => {
      expect(
        component.splitSummary({
          kind: 'anchored',
          initial_train_days: 90,
          test_days: 30,
          step_days: 30,
        }),
      ).toContain('90d initial');
    });
  });
});
