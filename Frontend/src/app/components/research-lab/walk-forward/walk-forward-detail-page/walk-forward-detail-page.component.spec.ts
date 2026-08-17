import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ExhaustiveRunService } from '../../../../services/exhaustive-run.service';
import { JobsService } from '../../../../services/jobs.service';
import { WalkForwardService } from '../../../../services/walk-forward.service';
import type {
  FoldResult,
  WalkForwardResponse,
} from '../../../../services/walk-forward.types';
import { WalkForwardDetailPageComponent } from './walk-forward-detail-page.component';

function makeMetrics(
  overrides: Partial<FoldResult['test_metrics']> = {},
): FoldResult['test_metrics'] {
  return {
    total_trades: 3,
    winning_trades: 2,
    losing_trades: 1,
    win_rate: 2 / 3,
    total_return_pct: 0.0234,
    max_drawdown_pct: 0.012,
    sharpe_ratio: 1.42,
    sortino_ratio: 1.78,
    profit_factor: 2.5,
    expectancy_pct: 0.008,
    payoff_ratio: 2,
    exposure_pct: 0.05,
    avg_trade_bars: 5,
    ...overrides,
  };
}

function makeFold(overrides: Partial<FoldResult> = {}): FoldResult {
  return {
    fold_index: 0,
    train_start_ms: 1704171600000,
    train_end_ms: 1709355600000,
    test_start_ms: 1709355600000,
    test_end_ms: 1711945200000,
    test_run_id: 'cccccccccccccccccccccccccccccccc',
    test_metrics: makeMetrics(),
    test_trade_count: 3,
    status: 'completed',
    failure_reason: null,
    selected_parameters: { gap_bps: 2 },
    training_candidates: [
      {
        parameters: { gap_bps: 2 },
        train_run_id: 'train-gap-2',
        train_metrics: makeMetrics({ sharpe_ratio: 1.42 }),
        receipt_persisted: true,
        eligible: true,
        ineligibility_reason: null,
      },
      {
        parameters: { gap_bps: 3 },
        train_run_id: 'train-gap-3',
        train_metrics: makeMetrics({ sharpe_ratio: 0.78 }),
        receipt_persisted: true,
        eligible: true,
        ineligibility_reason: null,
      },
    ],
    selected_train_sharpe: 1.42,
    oos_retention: null,
    ...overrides,
  };
}

function makeWfResponse(): WalkForwardResponse {
  return {
    config: {
      walk_forward_id: 'b'.repeat(32),
      parent_run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      strategy_spec_hash: 'd'.repeat(64),
      strategy_spec_json: { name: 'spy_ema_crossover' },
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
      parameter_search: {
        objective: 'sharpe_ratio',
        min_trades: 1,
        candidates: [
          { parameters: { gap_bps: 2 }, strategy_spec_hash: 'gap-2', strategy_spec_json: {} },
          { parameters: { gap_bps: 3 }, strategy_spec_hash: 'gap-3', strategy_spec_json: {} },
        ],
      },
      fold_position_policy: 'flat_at_test_boundaries',
      protocol_id: null,
      protocol_version: null,
      created_at_ms: 1736000000000,
    },
    result: {
      walk_forward_id: 'b'.repeat(32),
      parent_run_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      strategy_spec_hash: 'd'.repeat(64),
      split_policy: { kind: 'rolling', train_days: 60, test_days: 30, step_days: 30 },
      folds: [
        makeFold({ fold_index: 0 }),
        makeFold({
          fold_index: 1,
          test_run_id: 'dddddddddddddddddddddddddddddddd',
          selected_parameters: { gap_bps: 3 },
          test_metrics: makeMetrics({
            total_return_pct: -0.018,
            sharpe_ratio: -0.42,
          }),
        }),
      ],
      selection_failures: [],
      combined_oos_equity_curve: [
        { timestamp_ms: 1709355600000, equity: 100000 },
        { timestamp_ms: 1711945200000, equity: 102340 },
      ],
      mean_oos_sharpe: 0.5,
      median_oos_sharpe: 0.5,
      pct_profitable_folds: 0.5,
      oos_retention: null,
      oos_retention_basis: null,
      alpha_decay: -0.92,
      warnings: [],
      created_at_ms: 1736000000000,
      completed_at_ms: 1736000005000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

interface ServiceMock {
  getWalkForward: ReturnType<typeof vi.fn>;
  listWalkForwards: ReturnType<typeof vi.fn>;
  createWalkForward: ReturnType<typeof vi.fn>;
  runFromRun: ReturnType<typeof vi.fn>;
}

describe('WalkForwardDetailPageComponent', () => {
  let fixture: ComponentFixture<WalkForwardDetailPageComponent>;
  let component: WalkForwardDetailPageComponent;
  let service: ServiceMock;

  beforeEach(async () => {
    service = {
      getWalkForward: vi.fn().mockResolvedValue(makeWfResponse()),
      listWalkForwards: vi.fn(),
      createWalkForward: vi.fn(),
      runFromRun: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [WalkForwardDetailPageComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: WalkForwardService, useValue: service },
        {
          provide: ExhaustiveRunService,
          useValue: { getLatestForWalkForward: vi.fn().mockResolvedValue(null) },
        },
        {
          provide: JobsService,
          useValue: {
            jobs: signal([]),
            job: vi.fn(),
            startJob: vi.fn(),
            cancelJob: vi.fn(),
            fetchResult: vi.fn(),
          },
        },
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: of(convertToParamMap({ wf_id: 'b'.repeat(32) })),
          },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(WalkForwardDetailPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('reads wf_id from the route and fetches the walk-forward', () => {
    expect(service.getWalkForward).toHaveBeenCalledWith('b'.repeat(32));
    expect(component.wfId()).toBe('b'.repeat(32));
    expect(component.walkForward()?.result.folds).toHaveLength(2);
  });

  it('renders aggregate metrics from the server payload', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('OOS aggregates');
    // mean_oos_sharpe = 0.50 → '0.50'
    expect(text).toContain('0.50');
    // pct_profitable = 0.5 → '50%'
    expect(text).toContain('50%');
    // alpha_decay = -0.92 → '-0.920'
    expect(text).toContain('-0.920');
  });

  it('renders one row per fold with its run-id link', () => {
    const rows = fixture.nativeElement.querySelectorAll('.wfd-fold-table tbody tr');
    expect(rows.length).toBe(2);
    const firstRowText = (rows[0].textContent ?? '').trim();
    expect(firstRowText).toContain('cccccccccccc'); // truncated test_run_id
  });

  it('renders parameter stability and selected gap evidence', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('How each candidate behaved across folds');
    expect(text).toContain('Selected gap');
    expect(text).toContain('2 bps');
    expect(text).toContain('3 bps');
  });

  it('shows the parent-run link in the header when parent_run_id is set', () => {
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Parent run');
    expect(text).toContain('aaaaaaaaaaaaaaaa');
  });

  it('surfaces service errors', async () => {
    service.getWalkForward.mockRejectedValueOnce(new Error('not found'));
    await component.load('e'.repeat(32));
    fixture.detectChanges();
    expect(component.error()).toBe('not found');
  });

  describe('splitSummary', () => {
    it('formats rolling correctly', () => {
      expect(
        component.splitSummary({
          kind: 'rolling',
          train_days: 60,
          test_days: 30,
          step_days: 30,
        }),
      ).toContain('60d train');
    });
  });
});
