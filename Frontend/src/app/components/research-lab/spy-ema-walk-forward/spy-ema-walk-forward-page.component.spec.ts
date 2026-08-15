import { provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { screen } from '@testing-library/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { StrategyRunsService } from '../../../services/strategy-runs.service';
import { JobsService, type JobState } from '../../../services/jobs.service';
import type {
  RunLedger,
  StrategyRunResponse,
} from '../../../services/strategy-runs.types';
import { WalkForwardService } from '../../../services/walk-forward.service';
import type {
  WalkForwardConfig,
  WalkForwardResponse,
} from '../../../services/walk-forward.types';
import {
  SPY_EMA_EVIDENCE_CHART_FACTORY,
} from './spy-ema-evidence.component';
import { SpyEmaWalkForwardPageComponent } from './spy-ema-walk-forward-page.component';

const RUN_ID = 'a'.repeat(32);
const WALK_FORWARD_ID = 'b'.repeat(32);

function makeLedger(overrides: Partial<RunLedger> = {}): RunLedger {
  return {
    schema_version: '1.0',
    run_id: RUN_ID,
    parent_run_id: null,
    parent_spec_hash: null,
    strategy_spec_id: 'spy_ema_crossover',
    strategy_spec_hash: 'c'.repeat(64),
    strategy_spec_json: {
      name: 'SPY EMA(5)/EMA(10) Crossover with RSI gate',
    },
    engine_name: 'learn_ai_event_driven',
    engine_version: '0.1.0',
    engine_git_commit: 'test',
    symbol: 'SPY',
    resolution_minutes: 15,
    start_ms: 1_704_160_800_000,
    end_ms: 1_735_714_800_000,
    initial_cash: 100_000,
    fill_mode: 'signal_bar_close',
    commission_per_order: 0,
    slippage_per_share: 0,
    warmup_policy: 'spec_indicator_warmup',
    random_seed: 0,
    data_source: 'lean_minute_reader',
    data_snapshot_id: 'SPY|15|test',
    result_hash: 'd'.repeat(64),
    trade_log_hash: 'e'.repeat(64),
    metrics_hash: 'f'.repeat(64),
    created_at_ms: 1_736_000_000_000,
    completed_at_ms: 1_736_000_001_000,
    status: 'completed',
    failure_reason: null,
    ...overrides,
  };
}

function makeControl(): StrategyRunResponse {
  return {
    ledger: makeLedger(),
    result: {
      run_id: RUN_ID,
      initial_cash: 100_000,
      final_equity: 102_500,
      equity_curve: [
        { timestamp_ms: 1_704_160_800_000, equity: 100_000 },
        { timestamp_ms: 1_704_247_200_000, equity: 102_500 },
      ],
      drawdown_curve: [],
      trades: [],
      metrics: {
        total_trades: 12,
        winning_trades: 7,
        losing_trades: 5,
        win_rate: 7 / 12,
        total_return_pct: 0.025,
        max_drawdown_pct: 0.011,
        sharpe_ratio: 1.24,
        sortino_ratio: null,
        profit_factor: null,
        expectancy_pct: null,
        payoff_ratio: null,
        exposure_pct: null,
        avg_trade_bars: 5,
      },
      log_lines: [],
      warnings: [],
    },
  };
}

function makeWalkForwardConfig(): WalkForwardConfig {
  return {
    walk_forward_id: WALK_FORWARD_ID,
    parent_run_id: RUN_ID,
    strategy_spec_hash: 'c'.repeat(64),
    strategy_spec_json: { name: 'SPY EMA(5)/EMA(10) Crossover with RSI gate' },
    symbol: 'SPY',
    resolution_minutes: 15,
    start_ms: 1_704_160_800_000,
    end_ms: 1_735_714_800_000,
    initial_cash: 100_000,
    fill_mode: 'signal_bar_close',
    commission_per_order: 0,
    slippage_per_share: 0,
    random_seed: 0,
    split_policy: { kind: 'rolling', train_days: 180, test_days: 30, step_days: 30 },
    parameter_search: {
      objective: 'sharpe_ratio',
      min_trades: 5,
      candidates: [
        {
          parameters: { gap_bps: 1 },
          strategy_spec_hash: '4'.repeat(64),
          strategy_spec_json: { name: 'gap-1' },
        },
        {
          parameters: { gap_bps: 2 },
          strategy_spec_hash: '5'.repeat(64),
          strategy_spec_json: { name: 'gap-2' },
        },
      ],
    },
    fold_position_policy: 'flat_at_test_boundaries',
    protocol_id: 'spy-ema-normalized-gap',
    protocol_version: '1.0',
    created_at_ms: 1_736_000_002_000,
  };
}

function makeWalkForward(): WalkForwardResponse {
  const config = makeWalkForwardConfig();
  const metrics = makeControl().result.metrics;
  return {
    config,
    result: {
      walk_forward_id: WALK_FORWARD_ID,
      parent_run_id: RUN_ID,
      strategy_spec_hash: config.strategy_spec_hash,
      split_policy: config.split_policy,
      folds: [
        {
          fold_index: 0,
          train_start_ms: config.start_ms,
          train_end_ms: 1_719_892_800_000,
          test_start_ms: 1_719_979_200_000,
          test_end_ms: 1_722_571_200_000,
          test_run_id: '1'.repeat(32),
          test_metrics: { ...metrics, total_return_pct: 0.012 },
          test_trade_count: 4,
          status: 'completed',
          failure_reason: null,
          selected_parameters: { gap_bps: 1 },
          training_candidates: [],
          selected_train_sharpe: 1.1,
          oos_retention: 0.76,
        },
        {
          fold_index: 1,
          train_start_ms: 1_706_839_200_000,
          train_end_ms: 1_722_571_200_000,
          test_start_ms: 1_722_657_600_000,
          test_end_ms: 1_725_249_600_000,
          test_run_id: '2'.repeat(32),
          test_metrics: { ...metrics, total_return_pct: -0.004 },
          test_trade_count: 3,
          status: 'completed',
          failure_reason: null,
          selected_parameters: { gap_bps: 2 },
          training_candidates: [],
          selected_train_sharpe: 1.3,
          oos_retention: 0.65,
        },
      ],
      selection_failures: [],
      combined_oos_equity_curve: [
        { timestamp_ms: 1_719_979_200_000, equity: 100_000 },
        { timestamp_ms: 1_722_657_600_000, equity: 100_800 },
      ],
      mean_oos_sharpe: 0.84,
      median_oos_sharpe: 0.84,
      pct_profitable_folds: 0.5,
      oos_retention: null,
      oos_retention_basis: 'mean_fold_test_to_selected_train',
      alpha_decay: null,
      warnings: [],
      created_at_ms: config.created_at_ms,
      completed_at_ms: config.created_at_ms + 1_000,
      status: 'completed',
      failure_reason: null,
    },
  };
}

describe('SpyEmaWalkForwardPageComponent', () => {
  const series = { setData: vi.fn() };
  const chart = {
    addSeries: vi.fn().mockReturnValue(series),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    timeScale: vi.fn().mockReturnValue({ fitContent: vi.fn() }),
  };
  const chartFactory = vi.fn().mockReturnValue(chart);
  const strategyRuns = {
    listRuns: vi.fn(),
    getRun: vi.fn(),
  };
  const walkForwards = {
    listWalkForwards: vi.fn(),
    getWalkForward: vi.fn(),
  };
  const jobStates = signal<JobState[]>([]);
  const jobs = {
    jobs: jobStates.asReadonly(),
    job: vi.fn((id: string) => jobStates().find(job => job.id === id)),
    startJob: vi.fn(),
    cancelJob: vi.fn(),
    fetchResult: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    strategyRuns.listRuns.mockResolvedValue({ runs: [makeLedger()] });
    strategyRuns.getRun.mockResolvedValue(makeControl());
    walkForwards.listWalkForwards.mockResolvedValue({
      walk_forwards: [makeWalkForwardConfig()],
    });
    walkForwards.getWalkForward.mockResolvedValue(makeWalkForward());
    jobStates.set([]);
    jobs.startJob.mockImplementation(async () => {
      jobStates.set([{
        id: 'job-1',
        type: 'spy_ema_walk_forward',
        status: 'completed',
        recentLogs: [],
        logSeq: 0,
      }]);
      return 'job-1';
    });
    jobs.fetchResult.mockResolvedValue({
      control: makeControl(),
      walk_forward: makeWalkForward(),
    });

    TestBed.configureTestingModule({
      imports: [SpyEmaWalkForwardPageComponent],
      providers: [
        provideRouter([]),
        { provide: StrategyRunsService, useValue: strategyRuns },
        { provide: WalkForwardService, useValue: walkForwards },
        { provide: JobsService, useValue: jobs },
        { provide: SPY_EMA_EVIDENCE_CHART_FACTORY, useValue: chartFactory },
      ],
    });
  });

  it('renders the protocol and persisted control plus fold evidence', async () => {
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    expect(screen.getByRole('heading', {
      name: 'Can a normalized EMA gap survive out of sample?',
    })).toBeTruthy();
    expect(screen.getByRole('img', {
      name: /Six training months select one frozen threshold/,
    })).toBeTruthy();
    expect(screen.getByText('control neighbourhood')).toBeTruthy();
    expect(screen.getByText('2.50%')).toBeTruthy();
    expect(screen.getByText('1.24')).toBeTruthy();
    expect(screen.getByText('2/2', { selector: '.evidence__fold-summary strong' })).toBeTruthy();
    expect(screen.getByRole('img', {
      name: 'Combined selected-threshold OOS equity curve from persisted Python research results',
    })).toBeTruthy();
    expect(series.setData).toHaveBeenCalledWith([
      { time: 1_719_979_200, value: 100_000 },
      { time: 1_722_657_600, value: 100_800 },
    ]);
    expect(walkForwards.listWalkForwards).toHaveBeenCalledWith({
      protocol_id: 'spy-ema-normalized-gap',
      protocol_version: '1.0',
      limit: 1,
    });
    expect(strategyRuns.getRun).toHaveBeenCalledWith(RUN_ID);
    expect(strategyRuns.listRuns).not.toHaveBeenCalled();
  });

  it('runs the canonical 24-month pipeline and renders selected thresholds', async () => {
    walkForwards.listWalkForwards.mockResolvedValue({ walk_forwards: [] });
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.runStudy();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(jobs.startJob).toHaveBeenCalledWith('spy_ema_walk_forward', {});
    expect(jobs.fetchResult).toHaveBeenCalledWith('job-1');
    expect(screen.getByText('1 bps')).toBeTruthy();
    expect(screen.getByText('2 bps')).toBeTruthy();
    expect(screen.getByText(/chosen only from its trailing training window/)).toBeTruthy();
  });

  it('directs the researcher to create a control when none is persisted', async () => {
    walkForwards.listWalkForwards.mockResolvedValue({ walk_forwards: [] });
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    expect(screen.getByText('No completed canonical SPY EMA study found.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Go to Strategy Runs →' })).toBeTruthy();
    expect(strategyRuns.getRun).not.toHaveBeenCalled();
    expect(walkForwards.getWalkForward).not.toHaveBeenCalled();
  });

  it('cancels the active background study', async () => {
    jobs.startJob.mockImplementation(async () => {
      jobStates.set([{
        id: 'job-running',
        type: 'spy_ema_walk_forward',
        status: 'running',
        phase: 'walking_forward',
        phaseLabel: 'Selecting and testing each fold',
        recentLogs: [],
        logSeq: 0,
      }]);
      return 'job-running';
    });
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.runStudy();
    fixture.detectChanges();
    await fixture.componentInstance.cancelStudy();

    expect(jobs.cancelJob).toHaveBeenCalledWith('job-running');
  });

  it('keeps control evidence visible when the linked walk-forward lookup fails', async () => {
    walkForwards.getWalkForward.mockRejectedValue(new Error('fold ledger unavailable'));
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    expect(screen.getByText('2.50%')).toBeTruthy();
    expect(screen.getByText('fold ledger unavailable')).toBeTruthy();
    expect(screen.getByText('No normalized-gap study is persisted yet')).toBeTruthy();
  });

  it('renders failed evidence without substituting the control curve or a dead receipt link', async () => {
    const failed = makeWalkForward();
    failed.result = {
      ...failed.result,
      status: 'failed',
      failure_reason: 'fold 1 test receipt persistence failed',
      combined_oos_equity_curve: [],
      warnings: ['test receipt could not be persisted'],
      folds: [{
        ...failed.result.folds[0],
        status: 'failed',
        test_run_id: null,
        failure_reason: 'test receipt could not be persisted',
      }],
      selection_failures: [{
        fold_index: 1,
        train_start_ms: failed.config.start_ms,
        train_end_ms: failed.config.start_ms + 1,
        test_start_ms: failed.config.start_ms + 1,
        test_end_ms: failed.config.start_ms + 2,
        training_candidates: [],
        failure_reason: 'no eligible candidate',
      }],
    };
    walkForwards.getWalkForward.mockResolvedValue(failed);
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    expect(screen.getByText('Protocol failed closed')).toBeTruthy();
    expect(screen.getByText('The persisted result contains no equity points.')).toBeTruthy();
    expect(screen.queryByRole('link', { name: /Fold 1 return/ })).toBeNull();
    expect(screen.getByText(/test receipt could not be persisted/)).toBeTruthy();
  });

  it('does not accept a lookalike control identified only by its display name', async () => {
    strategyRuns.getRun.mockResolvedValue({
      ...makeControl(),
      ledger: makeLedger({ strategy_spec_id: 'not-the-canonical-fixture' }),
    });
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    expect(screen.getByText('The protocol parent is not the canonical SPY EMA control.')).toBeTruthy();
    expect(walkForwards.getWalkForward).not.toHaveBeenCalled();
  });

  it('ignores a newer custom walk-forward that is not protocol V1', async () => {
    const custom = {
      ...makeWalkForwardConfig(),
      walk_forward_id: '9'.repeat(32),
      protocol_id: null,
      protocol_version: null,
    };
    walkForwards.listWalkForwards.mockResolvedValue({
      walk_forwards: [custom, makeWalkForwardConfig()],
    });
    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.refresh();

    expect(walkForwards.getWalkForward).toHaveBeenCalledWith(WALK_FORWARD_ID);
    expect(walkForwards.getWalkForward).not.toHaveBeenCalledWith(custom.walk_forward_id);
  });

  it('discovers the protocol receipt before loading its linked control', async () => {
    const linkedControlId = '8'.repeat(32);
    const linkedConfig = {
      ...makeWalkForwardConfig(),
      parent_run_id: linkedControlId,
    };
    const linkedWalkForward = makeWalkForward();
    linkedWalkForward.config = linkedConfig;
    linkedWalkForward.result.parent_run_id = linkedControlId;
    walkForwards.listWalkForwards.mockResolvedValue({
      walk_forwards: [linkedConfig],
    });
    walkForwards.getWalkForward.mockResolvedValue(linkedWalkForward);
    strategyRuns.listRuns.mockResolvedValue({
      runs: [makeLedger({ run_id: '9'.repeat(32) })],
    });
    const linkedControl = makeControl();
    linkedControl.ledger.run_id = linkedControlId;
    linkedControl.result.run_id = linkedControlId;
    strategyRuns.getRun.mockResolvedValue(linkedControl);

    const fixture = TestBed.createComponent(SpyEmaWalkForwardPageComponent);
    fixture.detectChanges();
    await fixture.componentInstance.refresh();

    expect(strategyRuns.listRuns).not.toHaveBeenCalled();
    expect(strategyRuns.getRun).toHaveBeenCalledWith(linkedControlId);
    expect(walkForwards.getWalkForward).toHaveBeenCalledWith(WALK_FORWARD_ID);
  });
});
