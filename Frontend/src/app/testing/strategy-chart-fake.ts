import type { Mock, VitestUtils } from 'vitest';

/**
 * A lightweight-charts stand-in for the strategy chart's specs (#2639).
 *
 * jsdom has no canvas, so every spec that renders the strategy chart hands
 * `STRATEGY_CHART_FACTORY` one of these. Keeping one shape for all of them
 * means a chart call the component starts making is added here once, rather
 * than failing in whichever copy was missed.
 *
 * Each spec passes its own `vi`: a helper importing vitest at runtime lands
 * in a chunk evaluated before the spec's hoisted `vi.mock('lightweight-charts')`,
 * and the component then draws with the real library.
 */

export interface FakeSeries {
  readonly type: string;
  readonly options: Record<string, unknown>;
  readonly pane: number;
  readonly setData: Mock;
  readonly attachPrimitive: Mock;
  readonly createPriceLine: Mock;
}

export interface FakeClickAt {
  readonly point?: { readonly x: number; readonly y: number };
  readonly sourceEvent?: { readonly clientX: number; readonly clientY: number };
}

export interface FakeStrategyChart {
  /** What the chart factory returns to the component. */
  readonly chart: {
    readonly addSeries: Mock;
    readonly removeSeries: Mock;
    readonly timeScale: () => FakeStrategyChart['timeScale'];
    readonly panes: () => { readonly setStretchFactor: Mock }[];
    readonly applyOptions: Mock;
    readonly subscribeClick: Mock;
    readonly unsubscribeClick: Mock;
    readonly remove: Mock;
  };
  readonly timeScale: {
    readonly fitContent: Mock;
    readonly logicalToCoordinate: Mock;
    readonly width: () => number;
    readonly setVisibleLogicalRange: Mock;
    readonly getVisibleLogicalRange: Mock<() => { from: number; to: number } | null>;
    readonly subscribeSizeChange: Mock;
    readonly unsubscribeSizeChange: Mock;
    /** The library measuring its auto-sized canvas, a frame after creation. */
    readonly resize: (width: number) => void;
  };
  /** Every series the component added, in order; a removed one stays listed. */
  readonly series: FakeSeries[];
  /** The candlestick series. */
  candles(): FakeSeries;
  /** A click as the library reports it: `time` in chart seconds, `undefined` off any bar. */
  click(time: number | undefined, at?: FakeClickAt): void;
}

export function fakeStrategyChart(vi: VitestUtils, initialWidth = 800): FakeStrategyChart {
  const series: FakeSeries[] = [];
  const panes: { setStretchFactor: Mock }[] = [];
  let width = initialWidth;
  const timeScale: FakeStrategyChart['timeScale'] = {
    fitContent: vi.fn(),
    logicalToCoordinate: vi.fn(),
    width: () => width,
    setVisibleLogicalRange: vi.fn(),
    getVisibleLogicalRange: vi.fn((): { from: number; to: number } | null => ({ from: 0, to: 3 })),
    subscribeSizeChange: vi.fn(),
    unsubscribeSizeChange: vi.fn(),
    resize: (next) => {
      width = next;
      for (const [handler] of timeScale.subscribeSizeChange.mock.calls) handler(next, 300);
    },
  };
  const chart: FakeStrategyChart['chart'] = {
    addSeries: vi.fn((type: string, options: Record<string, unknown>, pane?: number) => {
      const created: FakeSeries = {
        type,
        options,
        pane: pane ?? 0,
        setData: vi.fn(),
        attachPrimitive: vi.fn(),
        createPriceLine: vi.fn(),
      };
      series.push(created);
      return created;
    }),
    removeSeries: vi.fn(),
    timeScale: () => timeScale,
    panes: () => {
      const count = Math.max(0, ...series.map((each) => each.pane)) + 1;
      while (panes.length < count) panes.push({ setStretchFactor: vi.fn() });
      return panes.slice(0, count);
    },
    applyOptions: vi.fn(),
    subscribeClick: vi.fn(),
    unsubscribeClick: vi.fn(),
    remove: vi.fn(),
  };
  return {
    chart,
    timeScale,
    series,
    candles: () => {
      const candles = series.find((each) => each.type === 'CandlestickSeries');
      if (candles === undefined) throw new Error('The chart drew no candles.');
      return candles;
    },
    click: (time, at = {}) => {
      const point = at.point ?? { x: 10, y: 10 };
      const sourceEvent = at.sourceEvent ?? { clientX: 200, clientY: 120 };
      for (const [handler] of chart.subscribeClick.mock.calls) handler({ time, point, sourceEvent });
    },
  };
}

/** A `STRATEGY_CHART_FACTORY` value that keeps every chart it creates, newest last. */
export function fakeStrategyChartFactory(vi: VitestUtils, initialWidth = 800) {
  const created: FakeStrategyChart[] = [];
  return {
    created,
    create: (): FakeStrategyChart['chart'] => {
      const fake = fakeStrategyChart(vi, initialWidth);
      created.push(fake);
      return fake.chart;
    },
    current: (): FakeStrategyChart => {
      const chart = created.at(-1);
      if (chart === undefined) throw new Error('No strategy chart was created.');
      return chart;
    },
  };
}
