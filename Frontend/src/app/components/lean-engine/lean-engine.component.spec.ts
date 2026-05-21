import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideZonelessChangeDetection } from '@angular/core';
import { describe, expect, it, vi } from 'vitest';

import {
  LeanEngineComponent,
  mapStudyTradeToEngineTrade,
  StudyTradeApiItem,
} from './lean-engine.component';
import { JobsService } from '../../services/jobs.service';
import { LeanSidecarService } from '../../services/lean-sidecar.service';
import type { TrustedRunRequest, TrustedRunResponse } from '../../services/lean-sidecar.types';

describe('mapStudyTradeToEngineTrade', () => {
  function makeItem(overrides: Partial<StudyTradeApiItem> = {}): StudyTradeApiItem {
    return {
      entryTimestamp: '2026-04-01T13:30:00Z',
      exitTimestamp: '2026-04-01T15:00:00Z',
      entryPrice: 500,
      exitPrice: 505,
      pnL: 5,
      signalReason: 'crossover',
      ...overrides,
    };
  }

  // Regression: prior to the fix, History → reload populated pnl_pct as 0
  // because the .NET BacktestTrade entity does not persist a percent column.
  // pnl_pct must be derived as pnl / entryPrice, matching the Python engine.
  it('derives pnl_pct from pnL / entryPrice when entry price is positive', () => {
    const trade = mapStudyTradeToEngineTrade(makeItem({ pnL: 5, entryPrice: 500 }), 0);
    expect(trade.pnl_pct).toBeCloseTo(0.01, 10);
    expect(trade.pnl_pts).toBe(5);
  });

  it('preserves the sign of pnl_pct for losing trades', () => {
    const trade = mapStudyTradeToEngineTrade(makeItem({ pnL: -1.705, entryPrice: 563.4 }), 0);
    expect(trade.pnl_pct).toBeCloseTo(-0.00302627, 8);
    expect(trade.result).toBe('LOSS');
  });

  it('falls back to 0 when entry price is zero or negative to avoid NaN/Infinity', () => {
    expect(mapStudyTradeToEngineTrade(makeItem({ entryPrice: 0, pnL: 1 }), 0).pnl_pct).toBe(0);
    expect(mapStudyTradeToEngineTrade(makeItem({ entryPrice: -5, pnL: 1 }), 0).pnl_pct).toBe(0);
  });

  it('passes through trade fields and assigns a 1-based trade_number', () => {
    const trade = mapStudyTradeToEngineTrade(
      makeItem({ pnL: 2.5, entryPrice: 100, signalReason: 'rsi_oversold' }),
      4,
    );
    expect(trade.trade_number).toBe(5);
    expect(trade.entry_time).toBe('2026-04-01T13:30:00Z');
    expect(trade.exit_time).toBe('2026-04-01T15:00:00Z');
    expect(trade.entry_price).toBe(100);
    expect(trade.exit_price).toBe(505);
    expect(trade.signal_reason).toBe('rsi_oversold');
    expect(trade.result).toBe('WIN');
    expect(trade.indicators).toEqual({});
  });

  it('handles missing signalReason as empty string', () => {
    const trade = mapStudyTradeToEngineTrade(makeItem({ signalReason: null }), 0);
    expect(trade.signal_reason).toBe('');
  });
});

describe('LeanEngineComponent.composeDataPolicy', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: JobsService,
          useValue: {
            startJob: () => Promise.resolve('job-id'),
            job: () => null,
            dismiss: () => undefined,
          },
        },
      ],
    });
  });

  it('synthesizes a canonical DataPolicy from current form state', () => {
    const fixture = TestBed.createComponent(LeanEngineComponent);
    const component = fixture.componentInstance;

    component.paramValues.set({ symbol: 'spy' });
    component.startDate.set('2025-01-13');
    component.endDate.set('2025-01-17');
    component.resolution.set('minute');
    component.initialCash.set(100_000);

    const dp = component.composeDataPolicy();

    expect(dp).toEqual({
      source: 'polygon',
      symbol: 'SPY',
      adjusted: true,
      session: 'regular',
      input_bars: { timespan: 'minute', multiplier: 1 },
      strategy_bars: { timespan: 'minute', multiplier: 1 },
      timestamp_policy: 'bar_close_ms_utc',
      timezone: 'America/New_York',
      provider_kind: 'live',
      fixture_id: null,
      fixture_sha256: null,
    });
  });

  it('maps the daily resolution to a day BarsSpec on both sides', () => {
    const fixture = TestBed.createComponent(LeanEngineComponent);
    const component = fixture.componentInstance;

    component.paramValues.set({ symbol: 'AAPL' });
    component.resolution.set('daily');

    const dp = component.composeDataPolicy();

    expect(dp.input_bars).toEqual({ timespan: 'day', multiplier: 1 });
    expect(dp.strategy_bars).toEqual({ timespan: 'day', multiplier: 1 });
  });

  it('falls back to SPY when no symbol is configured (matches effectiveSymbol)', () => {
    const fixture = TestBed.createComponent(LeanEngineComponent);
    const component = fixture.componentInstance;

    component.paramValues.set({});

    const dp = component.composeDataPolicy();

    expect(dp.symbol).toBe('SPY');
  });
});

describe('LeanEngineComponent engine selector', () => {
  // Mon 2025-01-21 09:30 EST = 14:30 UTC. Used as the resolved
  // exclusive-end millisecond when the operator picks 2025-01-17 (Fri)
  // as endDate — MLK Day (Mon 2025-01-20) is skipped.
  const NEXT_TRADING_DAY_OPEN_MS = Date.UTC(2025, 0, 21, 14, 30, 0);

  function configureTestBed(overrides: {
    startJob?: ReturnType<typeof vi.fn>;
    startTrustedRun?: ReturnType<typeof vi.fn>;
    nextTradingDayOpen?: ReturnType<typeof vi.fn>;
  } = {}) {
    const startJob =
      overrides.startJob ?? vi.fn().mockResolvedValue('job-id');
    const startTrustedRun =
      overrides.startTrustedRun ??
      vi.fn().mockResolvedValue({
        run_id: 'rid',
        is_clean: true,
        exit_code: 0,
        duration_ms: 0,
        timed_out: false,
        lean_errors: { analysis_failed: [], failed_data_requests: [], runtime_error: [], benchmark_unavailable: [], other: [] },
        log_tail: '',
        manifest_path: '/m',
        workspace_root: '/w',
        observations_path: '/o',
        lean_log_path: '/l',
        normalized_path: null,
        normalized_parser_version: null,
        total_order_events: null,
        total_equity_points: null,
        strategy_execution_id: null,
      } satisfies TrustedRunResponse);
    const nextTradingDayOpen =
      overrides.nextTradingDayOpen ??
      vi.fn().mockResolvedValue({
        next_trading_date: '2025-01-21',
        session_open_ms_utc: NEXT_TRADING_DAY_OPEN_MS,
      });

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: JobsService,
          useValue: {
            startJob,
            job: () => null,
            dismiss: () => undefined,
            fetchResult: () => Promise.resolve({}),
          },
        },
        {
          provide: LeanSidecarService,
          useValue: { startTrustedRun, nextTradingDayOpen },
        },
      ],
    });

    return { startJob, startTrustedRun, nextTradingDayOpen, NEXT_TRADING_DAY_OPEN_MS };
  }

  it('exposes an engine signal that defaults to python and accepts lean', () => {
    configureTestBed();
    const fixture = TestBed.createComponent(LeanEngineComponent);
    const component = fixture.componentInstance;

    expect(component.engine()).toBe('python');
    component.engine.set('lean');
    expect(component.engine()).toBe('lean');
  });

  it('exposes a leanSource signal seeded with the EMA-crossover template', () => {
    configureTestBed();
    const fixture = TestBed.createComponent(LeanEngineComponent);
    const component = fixture.componentInstance;

    expect(component.leanSource()).toContain('class MyAlgorithm');
  });

  it('submit routes to jobsService.startJob for the Python engine', async () => {
    const { startJob, startTrustedRun } = configureTestBed();
    const fixture = TestBed.createComponent(LeanEngineComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    component.engine.set('python');
    // Force a strategy so runPython()'s guard passes; the rest of the
    // payload is shape-checked below.
    component.strategies.set([
      {
        name: 'spy_ema_crossover',
        display_name: 'SPY EMA Crossover',
        description: '',
        params_schema: { properties: {} },
        supported_resolutions: ['minute'],
      },
    ]);
    component.selectedStrategyName.set('spy_ema_crossover');
    component.startDate.set('2025-01-13');
    component.endDate.set('2025-01-17');
    component.initialCash.set(100_000);

    await component.run();

    expect(startJob).toHaveBeenCalledTimes(1);
    expect(startTrustedRun).not.toHaveBeenCalled();
    const [kind, payload] = startJob.mock.calls[0] as [string, { backtest: Record<string, unknown> }];
    expect(kind).toBe('engine_backtest');
    expect(payload.backtest['strategy_name']).toBe('spy_ema_crossover');
    expect(payload.backtest['data_policy']).toMatchObject({
      source: 'polygon',
      symbol: 'SPY',
      adjusted: true,
    });
  });

  it('submit routes to leanSidecarService.startTrustedRun for the LEAN engine and includes algorithm_source', async () => {
    const { startJob, startTrustedRun, nextTradingDayOpen } = configureTestBed();
    const fixture = TestBed.createComponent(LeanEngineComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    component.engine.set('lean');
    component.leanSource.set('class MyAlgorithm: pass');
    component.startDate.set('2025-01-13');
    component.endDate.set('2025-01-17');
    component.initialCash.set(100_000);

    await component.run();

    expect(startTrustedRun).toHaveBeenCalledTimes(1);
    expect(startJob).not.toHaveBeenCalled();
    // The component must resolve the half-open exclusive end via the
    // server-side calendar before submitting; the operator's chosen
    // end date is the input to that call.
    expect(nextTradingDayOpen).toHaveBeenCalledTimes(1);
    expect(nextTradingDayOpen).toHaveBeenCalledWith('2025-01-17');

    const req = startTrustedRun.mock.calls[0][0] as TrustedRunRequest;
    expect(req.algorithm_source).toBe('class MyAlgorithm: pass');
    expect(req.starting_cash).toBe(100_000);
    expect(req.run_id).toMatch(/^engine_lab_spy_[a-z0-9]+$/);
    expect(req.start_ms_utc).toBe(component.composeStartMs());
    // end_ms_utc must be the NEXT trading day's session-open — Fri
    // 2025-01-17 → Tue 2025-01-21 (skipping MLK Mon 2025-01-20). The
    // half-open ``[start, end)`` contract makes 2025-01-17 trading
    // activity included rather than silently excluded.
    expect(req.end_ms_utc).toBe(Date.UTC(2025, 0, 21, 14, 30, 0));
    expect(req.end_ms_utc).not.toBe(component.composeEndMs());
    expect(req.data_policy).toMatchObject({
      source: 'polygon',
      symbol: 'SPY',
      adjusted: true,
    });
  });

  it('advances end_ms_utc past the user-picked end so single-day LEAN runs are not rejected as start == end', async () => {
    // Regression for the P1 finding on PR #307: when the operator
    // picks start_date == end_date the previous flow built start ==
    // end and the validator rejected the payload with a 422. The
    // calendar lookup must advance end_ms_utc to the next trading
    // session's open so the window is strictly positive.
    const nextOpen = Date.UTC(2025, 0, 14, 14, 30, 0); // Tue 2025-01-14 09:30 EST.
    const nextTradingDayOpen = vi.fn().mockResolvedValue({
      next_trading_date: '2025-01-14',
      session_open_ms_utc: nextOpen,
    });
    const { startTrustedRun } = configureTestBed({ nextTradingDayOpen });
    const fixture = TestBed.createComponent(LeanEngineComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    component.engine.set('lean');
    component.leanSource.set('class MyAlgorithm: pass');
    component.startDate.set('2025-01-13');
    component.endDate.set('2025-01-13');
    component.initialCash.set(100_000);

    await component.run();

    expect(nextTradingDayOpen).toHaveBeenCalledWith('2025-01-13');
    const req = startTrustedRun.mock.calls[0][0] as TrustedRunRequest;
    expect(req.start_ms_utc).toBe(Date.UTC(2025, 0, 13, 14, 30, 0));
    expect(req.end_ms_utc).toBe(nextOpen);
    expect(req.end_ms_utc).toBeGreaterThan(req.start_ms_utc);
  });

  it('composeStartMs/composeEndMs encode 09:30 ET as int64 ms UTC (EST=UTC-5)', () => {
    configureTestBed();
    const fixture = TestBed.createComponent(LeanEngineComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    component.startDate.set('2025-01-13');
    component.endDate.set('2025-01-17');

    // 2025-01-13 09:30 ET = 14:30 UTC = ms 1736778600000.
    expect(component.composeStartMs()).toBe(Date.UTC(2025, 0, 13, 14, 30, 0));
    expect(component.composeEndMs()).toBe(Date.UTC(2025, 0, 17, 14, 30, 0));
  });

  it('composeStartMs honors DST (EDT=UTC-4)', () => {
    configureTestBed();
    const fixture = TestBed.createComponent(LeanEngineComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    component.startDate.set('2025-07-15');

    // 2025-07-15 09:30 EDT = 13:30 UTC.
    expect(component.composeStartMs()).toBe(Date.UTC(2025, 6, 15, 13, 30, 0));
  });

  // ─── Bug A coverage — status messaging surface for benchmark-only "errors" ──
  //
  // After PR fix-lean-engine-lab-ui-bugs the server's classifier flips
  // ``is_clean`` back to True when the only errors are the benign SPY
  // default-benchmark cascade, so the existing
  // ``response.is_clean ? "completed" : "failed"`` mapping reports
  // "completed" without any further frontend logic. We augment the
  // *message* with a benchmark-missing note so the operator knows
  // alpha/beta zeros in the stats panel are a benchmark artifact,
  // not a strategy result.

  function leanResponse(overrides: Partial<TrustedRunResponse>): TrustedRunResponse {
    return {
      run_id: 'rid',
      is_clean: true,
      exit_code: 0,
      duration_ms: 0,
      timed_out: false,
      lean_errors: { analysis_failed: [], failed_data_requests: [], runtime_error: [], benchmark_unavailable: [], other: [] },
      log_tail: '',
      manifest_path: '/m',
      workspace_root: '/w',
      observations_path: '/o',
      lean_log_path: '/l',
      normalized_path: null,
      normalized_parser_version: null,
      total_order_events: null,
      total_equity_points: null,
      strategy_execution_id: null,
      ...overrides,
    };
  }

  async function runLeanWithResponse(response: TrustedRunResponse) {
    const startTrustedRun = vi.fn().mockResolvedValue(response);
    configureTestBed({ startTrustedRun });
    const fixture = TestBed.createComponent(LeanEngineComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;
    component.engine.set('lean');
    component.leanSource.set('class MyAlgorithm: pass');
    component.startDate.set('2025-01-13');
    component.endDate.set('2025-01-17');
    component.initialCash.set(100_000);
    await component.run();
    return component;
  }

  it('status banner surfaces the benchmark-missing note when is_clean and benchmark_unavailable is non-empty', async () => {
    const component = await runLeanWithResponse(leanResponse({
      run_id: 'engine_lab_spy_abc',
      is_clean: true,
      lean_errors: {
        analysis_failed: [],
        failed_data_requests: [],
        runtime_error: [],
        benchmark_unavailable: ['SubscriptionDataSourceReader.InvalidSource: daily/spy.zip'],
        other: [],
      },
    }));
    expect(component.runPhase()).toBe('completed');
    expect(component.runStatusBanner()).toContain('engine_lab_spy_abc');
    expect(component.runStatusBanner()).toContain('SPY benchmark was unavailable');
  });

  it('status banner stays terse when is_clean and no error buckets are populated', async () => {
    const component = await runLeanWithResponse(leanResponse({
      run_id: 'engine_lab_spy_clean',
      is_clean: true,
    }));
    expect(component.runPhase()).toBe('completed');
    expect(component.runStatusBanner()).toContain('engine_lab_spy_clean');
    expect(component.runStatusBanner()).not.toContain('SPY benchmark was unavailable');
  });

  it('status banner reports failed and includes the exit code when is_clean is false', async () => {
    const component = await runLeanWithResponse(leanResponse({
      run_id: 'engine_lab_spy_fail',
      is_clean: false,
      exit_code: 1,
    }));
    expect(component.runPhase()).toBe('failed');
    expect(component.runStatusBanner()).toContain('exit 1');
    expect(component.runStatusBanner()).not.toContain('SPY benchmark was unavailable');
  });
});
