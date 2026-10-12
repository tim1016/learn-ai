import type { Mock, VitestUtils } from 'vitest';

/**
 * A lightweight-charts stand-in for the one-day chart's specs. The wrapper is given it through `TIMELINE_CHART_FACTORY`, so
 * a spec reads what the wrapper handed to the library: the options the chart was made with, every series added (its type,
 * options and pane) and the data set on it. It takes the spec's own `vi`, as the shared strategy-chart fake does: a helper
 * importing vitest at runtime lands in a chunk evaluated before the spec's hoisted `vi.mock('lightweight-charts')`.
 */

export interface FakeTimelineSeries {
  readonly type: string;
  readonly options: Record<string, unknown>;
  readonly pane: number;
  readonly setData: Mock;
}

export interface CrosshairParam {
  readonly time?: number | string;
  readonly point?: { readonly x: number; readonly y: number };
}

export interface FakeTimelineChart {
  /** What the factory returns to the wrapper. */
  readonly chart: {
    readonly addSeries: Mock;
    readonly removeSeries: Mock;
    readonly timeScale: () => { readonly fitContent: Mock };
    readonly subscribeCrosshairMove: Mock;
    readonly unsubscribeCrosshairMove: Mock;
    readonly remove: Mock;
  };
  /** The factory: records the element and the options each chart was made with. */
  readonly factory: (container: HTMLElement, options: Record<string, unknown>) => FakeTimelineChart['chart'];
  readonly created: { readonly container: HTMLElement; readonly options: Record<string, unknown> }[];
  /** Every series added, in order; a removed one stays listed. */
  readonly series: FakeTimelineSeries[];
  readonly fitContent: Mock;
  /** The crosshair moving, as the library reports it to everyone who subscribed. */
  crosshair(param: CrosshairParam): void;
}

export function fakeTimelineChart(vi: Pick<VitestUtils, 'fn'>): FakeTimelineChart {
  const series: FakeTimelineSeries[] = [];
  const created: FakeTimelineChart['created'] = [];
  const fitContent = vi.fn();
  const chart: FakeTimelineChart['chart'] = {
    addSeries: vi.fn((type: string, options: Record<string, unknown>, pane?: number) => {
      const each: FakeTimelineSeries = { type, options, pane: pane ?? 0, setData: vi.fn() };
      series.push(each);
      return each;
    }),
    removeSeries: vi.fn(),
    timeScale: () => ({ fitContent }),
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
    remove: vi.fn(),
  };
  return {
    chart,
    factory: (container, options) => {
      created.push({ container, options });
      return chart;
    },
    created,
    series,
    fitContent,
    crosshair: (param) => {
      for (const [handler] of chart.subscribeCrosshairMove.mock.calls) handler({ seriesData: new Map(), ...param });
    },
  };
}
