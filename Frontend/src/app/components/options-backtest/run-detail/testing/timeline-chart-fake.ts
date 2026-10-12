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

/** The part of the library's time scale the wrapper uses. */
export interface FakeTimeScale {
  readonly fitContent: Mock;
  readonly setVisibleLogicalRange: Mock;
  readonly width: () => number;
  readonly subscribeSizeChange: Mock;
  readonly unsubscribeSizeChange: Mock;
}

export interface FakeTimelineChart {
  /** What the factory returns to the wrapper. */
  readonly chart: {
    readonly addSeries: Mock;
    readonly removeSeries: Mock;
    readonly timeScale: () => FakeTimeScale;
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
  /** The ranges the wrapper asked the time scale to show, in order. */
  readonly setVisibleLogicalRange: Mock;
  /** The width of the plot changing, as the library reports it to everyone who subscribed. */
  resize(widthPx: number): void;
  /** The crosshair moving, as the library reports it to everyone who subscribed. */
  crosshair(param: CrosshairParam): void;
}

/** `widthPx` is the width of the plot when the chart is made: about what the page gives it (0 is a chart not yet laid out). */
export function fakeTimelineChart(vi: Pick<VitestUtils, 'fn'>, widthPx = 600): FakeTimelineChart {
  const series: FakeTimelineSeries[] = [];
  const created: FakeTimelineChart['created'] = [];
  const fitContent = vi.fn();
  const setVisibleLogicalRange = vi.fn();
  let width = widthPx;
  const timeScale: FakeTimeScale = {
    fitContent,
    setVisibleLogicalRange,
    width: () => width,
    subscribeSizeChange: vi.fn(),
    unsubscribeSizeChange: vi.fn(),
  };
  const chart: FakeTimelineChart['chart'] = {
    addSeries: vi.fn((type: string, options: Record<string, unknown>, pane?: number) => {
      const each: FakeTimelineSeries = { type, options, pane: pane ?? 0, setData: vi.fn() };
      series.push(each);
      return each;
    }),
    removeSeries: vi.fn(),
    timeScale: () => timeScale,
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
    setVisibleLogicalRange,
    resize: (next) => {
      width = next;
      for (const [handler] of timeScale.subscribeSizeChange.mock.calls) handler(next, 0);
    },
    crosshair: (param) => {
      for (const [handler] of chart.subscribeCrosshairMove.mock.calls) handler({ seriesData: new Map(), ...param });
    },
  };
}
