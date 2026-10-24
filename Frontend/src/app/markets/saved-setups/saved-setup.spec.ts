import { describe, expect, it } from 'vitest';

import type { DataLabCompanionSettings } from '../../components/data-lab/data-lab-workspace-store';
import type { DataLabSession, DataLabSessionChartSnapshot } from '../../services/data-lab-session.service';
import { isShowableChart, planSetupOpening } from './saved-setup';

const COMPANIONS: DataLabCompanionSettings = {
  optionsCompanionEnabled: false,
  includeQualityReport: true,
  includePreviousClose: false,
  includeSplits: false,
  includeDividends: false,
  includeTickerOverview: false,
  includeNews: false,
  includeFinancials: false,
  includeStockTrades: false,
  includeStockQuotes: false,
};

function chart(overrides: Record<string, unknown> = {}): DataLabSessionChartSnapshot {
  return {
    timeframe: '5m',
    bars: [],
    indicators: [],
    quality: { gaps_found: 0 },
    allowedTimeframes: ['5m'],
    estimatedBarsPerTimeframe: {},
    recommendedTimeframe: '5m',
    visibleIndicatorIds: [],
    ...overrides,
  } as unknown as DataLabSessionChartSnapshot;
}

function setup(overrides: Partial<DataLabSession['config']> = {}, chartSnapshot: unknown = chart()): DataLabSession {
  return {
    id: 'session-1',
    name: 'Apple review',
    createdAtMs: Date.UTC(2026, 4, 15),
    updatedAtMs: Date.UTC(2026, 4, 15),
    config: {
      ticker: ' aapl ',
      fromDate: '2026-04-15',
      toDate: '2026-05-15',
      session: 'extended',
      forwardFill: false,
      adjusted: true,
      entries: [{ name: 'ema', params: { length: 20 } }],
      ...overrides,
    },
    chartSnapshot: chartSnapshot as DataLabSession['chartSnapshot'],
  };
}

describe('planSetupOpening', () => {
  it('opens a record saved on raw prices on the raw tree, in its address, and reads a record without the choices as the defaults', () => {
    expect(planSetupOpening(setup({ adjusted: false }), '1d', COMPANIONS).address).toMatchObject({ adjustment: 'raw' });

    const { adjusted: _adjusted, forwardFill: _forwardFill, ...rest } = setup().config;
    const bare = planSetupOpening({ ...setup(), config: rest as DataLabSession['config'] }, '1d', COMPANIONS);
    expect(bare.address).toMatchObject({ adjustment: 'polygon_split_adjusted', forwardFill: true });
  });

  it('turns a whole record into the v2 envelope, the address it opens on and the chart it carries, with nothing to say', () => {
    const plan = planSetupOpening(setup(), '1d', COMPANIONS);

    expect(plan.ticker).toBe('AAPL');
    expect(plan.envelope).toMatchObject({
      schemaVersion: 2,
      ticker: 'AAPL',
      windowMsUtc: { startMsUtc: Date.UTC(2026, 3, 15), endMsUtc: Date.UTC(2026, 4, 15) + 86_400_000 - 1 },
      scope: { timeframe: '5m', timespan: 'minute', multiplier: 5, session: 'extended', forwardFill: false, adjusted: true },
      indicators: [{ canonicalKey: 'ema', params: { length: 20 } }],
      savedSession: { id: 'session-1', schemaVersion: 2 },
      companions: COMPANIONS,
    });
    expect(plan.address).toEqual({
      window: { from: '2026-04-15', to: '2026-05-15' },
      timeframe: '5m',
      session: 'extended',
      adjustment: 'polygon_split_adjusted',
      forwardFill: false,
    });
    expect(plan.chart).not.toBeNull();
    expect(plan.notes).toEqual([]);
  });

  it('opens a record saved without a chart at the timeframe on screen, since only the chart carries one', () => {
    const plan = planSetupOpening(setup({}, null), '15m', COMPANIONS);

    expect(plan.address.timeframe).toBe('15m');
    expect(plan.chart).toBeNull();
    expect(plan.notes).toEqual([]);
  });

  it('keeps the daily timeframe in the store\'s own vocabulary', () => {
    const plan = planSetupOpening(setup({}, chart({ timeframe: '1D' })), '5m', COMPANIONS);

    expect(plan.envelope['scope']).toMatchObject({ timeframe: 'day', timespan: 'day', multiplier: 1 });
    expect(plan.address.timeframe).toBe('1d');
  });

  it('does not draw a chart saved at a timeframe the place does not offer, says so, and opens at the one on screen', () => {
    const plan = planSetupOpening(setup({}, chart({ timeframe: '2m' })), '1h', COMPANIONS);

    expect(plan.chart).toBeNull();
    expect(plan.address.timeframe).toBe('1h');
    expect(plan.notes).toHaveLength(1);
    expect(plan.notes[0]).toContain('2m');
    expect(plan.notes[0]).toContain('stays in the setup');
  });

  it('does not draw a chart missing a part the chart draws from, and says so', () => {
    const { bars: _bars, ...withoutBars } = chart();
    const plan = planSetupOpening(setup({}, withoutBars), '1d', COMPANIONS);

    expect(plan.chart).toBeNull();
    expect(plan.notes[0]).toContain('not in a shape this page can show');
  });

  it('opens a record whose dates are not dates on the default window, and says so', () => {
    const plan = planSetupOpening(setup({ fromDate: '2026-4-1', toDate: 'soon' }, null), '1d', COMPANIONS);

    expect(plan.address.window).toBeNull();
    expect(plan.envelope).not.toHaveProperty('windowMsUtc');
    expect(plan.notes[0]).toContain('not both dates');
  });

  it('hands entries the store\'s validator has to judge through as they are, so it can name what it dropped', () => {
    const plan = planSetupOpening(setup({ entries: [{ name: 'ema', params: { length: 20 } }, 'junk', { params: {} }] as never }, null), '1d', COMPANIONS);

    expect(plan.envelope['indicators']).toEqual([
      { canonicalKey: 'ema', params: { length: 20 } },
      'junk',
      { canonicalKey: undefined, params: {} },
    ]);
  });
});

describe('isShowableChart', () => {
  it('accepts the saved chart a setup carries and refuses anything missing a part', () => {
    expect(isShowableChart(chart())).toBe(true);
    expect(isShowableChart(null)).toBe(false);
    expect(isShowableChart([])).toBe(false);
    expect(isShowableChart(chart({ visibleIndicatorIds: 'ema' }))).toBe(false);
    expect(isShowableChart(chart({ quality: null }))).toBe(false);
  });
});
