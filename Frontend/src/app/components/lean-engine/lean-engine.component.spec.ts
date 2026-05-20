import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';

import {
  LeanEngineComponent,
  mapStudyTradeToEngineTrade,
  StudyTradeApiItem,
} from './lean-engine.component';
import { JobsService } from '../../services/jobs.service';

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
