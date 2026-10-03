import type { Provider } from '@angular/core';

import { GOLDEN_SEARCH_CHART_THEME, type ChartTheme } from '../charts/golden-search-chart-theme';
import { GOLDEN_SEARCH_CHARTS, type ChartAction, type ChartInstance, type ChartLibrary, type ChartOption } from '../charts/golden-search-echarts';

/** One chart the fake library made: what it was drawn with and asked to do. */
export interface FakeChart {
  readonly element: HTMLElement;
  readonly options: ChartOption[];
  readonly actions: ChartAction[];
  resizes: number;
  clears: number;
  disposed: boolean;
}

/** Fixed colours in place of the app's tokens, which specs do not load. */
export const FAKE_CHART_THEME: ChartTheme = {
  text: '#d1d4dc',
  textSecondary: '#b2b5be',
  axis: '#2a2e39',
  gridLine: '#1e222d',
  tooltipBackground: '#1b1f2e',
  warn: '#ff9800',
  loss: '#ef5350',
  gain: '#26a69a',
  neutral: '#1b1f2e',
  tooFew: '#2a2e39',
  stepBelow: '#4fc3f7',
  stepAbove: '#2962ff',
  stressed: '#26a69a',
  withoutBest: '#26a69a',
  development: '#26a69a',
  candidates: { incumbent: '#b2b5be', all_period: '#2962ff', recent: '#ffb300' },
};

/**
 * A stand-in for ECharts behind `GOLDEN_SEARCH_CHARTS`: it records every
 * draw, action, resize, clear and dispose, so specs check what a chart was told
 * without loading the library or a canvas, drawn in fixed colours.
 */
export function fakeCharts(): { readonly charts: FakeChart[]; readonly providers: Provider[] } {
  const charts: FakeChart[] = [];
  const library: ChartLibrary = {
    init(element: HTMLElement): ChartInstance {
      const chart: FakeChart = { element, options: [], actions: [], resizes: 0, clears: 0, disposed: false };
      charts.push(chart);
      return {
        draw: (option) => chart.options.push(option),
        resize: () => chart.resizes++,
        dispatch: (action) => chart.actions.push(action),
        clear: () => chart.clears++,
        dispose: () => (chart.disposed = true),
      };
    },
  };
  return {
    charts,
    providers: [
      { provide: GOLDEN_SEARCH_CHARTS, useValue: () => Promise.resolve(library) },
      { provide: GOLDEN_SEARCH_CHART_THEME, useValue: () => FAKE_CHART_THEME },
    ],
  };
}
