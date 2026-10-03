import { InjectionToken } from '@angular/core';
import type { BarSeriesOption, HeatmapSeriesOption, LineSeriesOption, ScatterSeriesOption } from 'echarts/charts';
import type {
  AxisPointerComponentOption,
  GridComponentOption,
  MarkLineComponentOption,
  TooltipComponentOption,
  VisualMapComponentOption,
} from 'echarts/components';
import type { ComposeOption } from 'echarts/core';

/** The ECharts option shape the Golden Search charts may use; a chart type joins it when its loader registers it. */
export type ChartOption = ComposeOption<
  | LineSeriesOption
  | BarSeriesOption
  | ScatterSeriesOption
  | HeatmapSeriesOption
  | GridComponentOption
  | TooltipComponentOption
  | AxisPointerComponentOption
  | MarkLineComponentOption
  | VisualMapComponentOption
>;

/** An ECharts action (`highlight`, `downplay`, `showTip`, `hideTip`). */
export interface ChartAction {
  readonly type: string;
  readonly [key: string]: unknown;
}

/** The slice of an ECharts instance the chart host drives. */
export interface ChartInstance {
  draw(option: ChartOption): void;
  resize(): void;
  dispatch(action: ChartAction): void;
  /** Removes everything drawn, keeping the instance for the next draw. */
  clear(): void;
  dispose(): void;
}

export interface ChartLibrary {
  init(element: HTMLElement): ChartInstance;
}

let loading: Promise<ChartLibrary> | null = null;

/**
 * ECharts through its modular core, with only the chart types and components
 * the Golden Search charts use (registered in the setup module), loaded on
 * first draw so the library ships in its own chunk rather than the app's
 * initial bundle.
 */
export function loadGoldenSearchCharts(): Promise<ChartLibrary> {
  loading ??= import('./golden-search-echarts-setup').then(
    ({ init }) => ({
      init(element: HTMLElement): ChartInstance {
        const chart = init(element, null, { renderer: 'canvas' });
        return {
          draw: (option) => chart.setOption(option, { notMerge: true }),
          resize: () => chart.resize(),
          dispatch: (action) => chart.dispatchAction(action),
          clear: () => chart.clear(),
          dispose: () => chart.dispose(),
        };
      },
    }),
    (error: unknown) => {
      // A failed chunk load must not stick: the next chart tries again.
      loading = null;
      throw error;
    },
  );
  return loading;
}

/** Swapped for a fake in tests, so specs never load ECharts or need a canvas. */
export const GOLDEN_SEARCH_CHARTS = new InjectionToken<() => Promise<ChartLibrary>>('GOLDEN_SEARCH_CHARTS', {
  providedIn: 'root',
  factory: () => loadGoldenSearchCharts,
});
