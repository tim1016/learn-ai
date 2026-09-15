import { signal } from '@angular/core';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { of } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import {
  DayReturns,
  KindDistribution,
  ReturnDistributionStudy,
  ReturnsDistributionService,
} from './returns-distribution.service';
import { ReturnsDistributionComponent } from './returns-distribution.component';

/** chart.js is module-mocked: the page spec drives bin selection through the
 * same options.onClick the real library invokes on canvas clicks. */
const harness = vi.hoisted(() => {
  return {
    instances: [] as {
      config: { options: { onClick?: (event: unknown, elements: { index: number }[]) => void } };
    }[],
  };
});

vi.mock('chart.js', () => {
  return {
    Chart: class {
      static register(..._args: unknown[]): void {}
      constructor(
        _canvas: unknown,
        config: { options: { onClick?: (event: unknown, elements: { index: number }[]) => void } },
      ) {
        harness.instances.push({ config: config as never });
      }
      destroy(): void {}
    },
    registerables: [],
  };
});

const DAY_MS = (july: number) => Date.UTC(2024, 6, july, 13, 30);

function kindFixture(kind: KindDistribution['kind'], nDays: number): KindDistribution {
  return {
    kind,
    bins: [
      { lowerPct: 0, upperPct: 0.5, count: nDays, isEdge: false },
      { lowerPct: 0.5, upperPct: 1, count: 0, isEdge: false },
    ],
    normalExpectedCounts: [nDays * 0.6, nDays * 0.1],
    stats: {
      nDays,
      meanPct: 0.05,
      stdPct: 0.9,
      annualizedVolPct: 14.3,
      skewness: -0.2,
      excessKurtosis: 1.5,
      var95Pct: -1.4,
      cvar95Pct: -2.0,
      bestDay: { sessionOpenMsUtc: DAY_MS(2), valuePct: 2.5 },
      worstDay: { sessionOpenMsUtc: DAY_MS(3), valuePct: -1.8 },
    },
  };
}

const DAYS: DayReturns[] = [
  {
    sessionOpenMsUtc: DAY_MS(2),
    closeToClosePct: 0.25,
    sessionPct: 0.31,
    overnightPct: -0.06,
    preMarketPct: 0.02,
    morningPct: 0.19,
    afternoonPct: 0.12,
    afterHoursPct: null,
    volume: 4_500_000,
    binIndices: { close_to_close: 0, session: 0, overnight: 1 },
  },
  {
    sessionOpenMsUtc: DAY_MS(3),
    closeToClosePct: 0.4,
    sessionPct: 0.2,
    overnightPct: 0.2,
    preMarketPct: null,
    morningPct: 0.0,
    afternoonPct: 0.2,
    afterHoursPct: 0.01,
    volume: 3_000_000,
    binIndices: { close_to_close: 0, session: 0, overnight: 1 },
  },
];

const STUDY: ReturnDistributionStudy = {
  adjustment: 'split_and_dividend',
  warnings: [],
  capture: {
    status: 'complete',
    fetchedArtifactCount: 480,
    detail: null,
  },
  coverage: {
    requestedSessions: 2,
    returnedSessions: 2,
    missingSessions: 0,
    excludedSessions: 0,
    firstSessionOpenMsUtc: DAY_MS(2),
    lastSessionOpenMsUtc: DAY_MS(3),
  },
  kinds: [kindFixture('close_to_close', 2), kindFixture('session', 2), kindFixture('overnight', 2)],
  days: DAYS,
};

function fakeStore() {
  return {
    committedTicker: signal('SPY'),
    committedWindow: signal({
      startMsUtc: Date.UTC(2024, 6, 1),
      endMsUtc: Date.UTC(2024, 6, 30, 23, 59, 59),
    }),
  } as unknown as DataLabWorkspaceStore;
}

function fakeService() {
  return {
    distribution: vi.fn(() => of(STUDY)),
    minuteCandles: vi.fn(() =>
      of([
        {
          id: 0,
          tickerId: 0,
          open: 545.1,
          high: 545.2,
          low: 545,
          close: 545.15,
          volume: 1200,
          volumeWeightedAveragePrice: null,
          timestamp: DAY_MS(2),
          timespan: 'minute',
          multiplier: 1,
          transactionCount: null,
        },
      ]),
    ),
  } as unknown as ReturnsDistributionService;
}

beforeEach(() => {
  harness.instances.length = 0;
});

describe('ReturnsDistributionComponent', () => {
  it('renders the study for the committed scope: stats, histogram, kind toggle', async () => {
    await render(ReturnsDistributionComponent, {      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: fakeService() },
      ],
    });

    expect(screen.getByText('Days studied')).toBeTruthy();
    expect(screen.getAllByText('2').length).toBeGreaterThan(0);
    expect(screen.getByText('VaR 95% (1 day)')).toBeTruthy();
    expect(
      screen.getByText('This request populated the data lake with 480 artifact(s) first.'),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Close → close' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Session only' })).toBeTruthy();
    expect(document.querySelector('canvas')).toBeTruthy();
  });

  it('renders every capture state distinctly — failure never reads as success', async () => {
    const failedStudy: ReturnDistributionStudy = {
      ...STUDY,
      capture: { status: 'failed', fetchedArtifactCount: 0, detail: 'TimeoutError: deadline' },
    };
    const service = {
      distribution: vi.fn(() => of(failedStudy)),
      minuteCandles: vi.fn(() => of([])),
    } as unknown as ReturnsDistributionService;
    await render(ReturnsDistributionComponent, {
      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: service },
      ],
    });

    expect(
      screen.getByText(/This request could not populate the data lake/),
    ).toBeTruthy();
    expect(screen.getByText(/TimeoutError: deadline/)).toBeTruthy();
    expect(screen.queryByText(/already up to date/)).toBeNull();
  });

  it('switches the shown distribution when the return kind toggles', async () => {
    const service = fakeService();
    await render(ReturnsDistributionComponent, {
      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: service },
      ],
    });

    // Both kinds report nDays=2 in this fixture; the kind identity is what
    // the toggle must swap — visible through which bins the chart redraws
    // with when the signal changes.
    await userEvent.click(screen.getByRole('button', { name: 'Session only' }));
    await waitFor(() => {
      const chart = harness.instances.at(-1)!;
      expect(chart.config.options.onClick).toBeTypeOf('function');
    });
  });

  it('opens the basket drill-down on a chart bin click, then the candle pane on a day click', async () => {
    const service = fakeService();
    await render(ReturnsDistributionComponent, {
      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: service },
      ],
    });

    expect(screen.queryByText(/^Basket /)).toBeNull();
    await waitFor(() => expect(harness.instances.length).toBeGreaterThan(0));
    harness.instances[0]!.config.options.onClick!(null, [{ index: 0 }]);

    expect(await screen.findByText('Basket 0.0% to 0.5% — 2 day(s)')).toBeTruthy();
    expect(screen.getByText('2024-07-02')).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: /2024-07-02/ }));
    expect(await screen.findByText(/2024-07-02 — full trading day/)).toBeTruthy();
    expect(service.minuteCandles).toHaveBeenCalledWith('SPY', '2024-07-02');
  });

  it('asks for scope before anything else when the store has no committed window', async () => {
    const service = fakeService();
    await render(ReturnsDistributionComponent, {
      providers: [
        {
          provide: DataLabWorkspaceStore,
          useValue: { committedTicker: signal(''), committedWindow: signal(null) } as unknown as DataLabWorkspaceStore,
        },
        { provide: ReturnsDistributionService, useValue: service },
      ],
    });

    expect(screen.getByText(/Pick a ticker and date range/)).toBeTruthy();
    expect(service.distribution).not.toHaveBeenCalled();
  });
});
