import type { ChartBar } from '../lib/broker-v2-panel.types';
import type { components } from '../../../../api/broker.types';
import type {
  ChartIndicatorEntry,
  ChartIndicatorPoint,
  ChartIndicatorResult,
  TradingIndicatorChip,
} from '../../../../shared/trading-chart';

const INDICATOR_COLORS = [
  '#ffb300', '#7aa9ff', '#ab47bc', '#26a69a', '#ec407a', '#ff6d00',
] as const;

export interface SelectedChartIndicator extends ChartIndicatorEntry, TradingIndicatorChip {}

export type ChartIndicatorRequestBar = components['schemas']['ChartIndicatorBar'];
export type ChartIndicatorBatchResponse = components['schemas']['ChartIndicatorBatchResponse'];

export interface IndicatorSeriesPlan {
  id: string;
  pane: string;
  type: 'line' | 'histogram';
  color: string;
  points: { time: number; value: number }[];
  referenceLevels: readonly number[];
}

export function selectChartIndicator(
  current: readonly SelectedChartIndicator[],
  entry: ChartIndicatorEntry,
): readonly SelectedChartIndicator[] {
  const id = indicatorRecipeId(entry);
  if (current.some((indicator) => indicator.id === id)) return current;
  return [
    ...current,
    {
      ...entry,
      params: { ...entry.params },
      id,
      label: indicatorLabel(entry),
      color: INDICATOR_COLORS[current.length % INDICATOR_COLORS.length],
    },
  ];
}

export function indicatorRecipeId(entry: ChartIndicatorEntry): string {
  const params = Object.entries(entry.params)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, value]) => `${name}:${value}`)
    .join('|');
  return params ? `${entry.name}|${params}` : entry.name;
}

export function toChartIndicatorRequestBars(
  bars: readonly ChartBar[],
): ChartIndicatorRequestBar[] {
  return bars.map((bar) => ({
    t: bar.end_ms,
    o: Number(bar.open),
    h: Number(bar.high),
    l: Number(bar.low),
    c: Number(bar.close),
    v: Number(bar.volume),
  }));
}

export function toIndicatorSeriesPlans(
  results: readonly ChartIndicatorResult[],
  bars: readonly ChartBar[],
): IndicatorSeriesPlan[] {
  const chartTimes = new Map(bars.map((bar) => [bar.end_ms, bar.start_ms / 1_000]));
  return results.flatMap((result) => {
    if (Array.isArray(result.data)) {
      return [seriesPlan(result, result.id, result.data, result.type === 'histogram', chartTimes, result.refs ?? [])];
    }
    return Object.entries(result.data).map(([name, points], index) =>
      seriesPlan(
        result,
        `${result.id}-${name}`,
        points,
        name.toLowerCase().includes('histogram'),
        chartTimes,
        index === 0 ? result.refs ?? [] : [],
      ),
    );
  });
}

function seriesPlan(
  result: ChartIndicatorResult,
  id: string,
  points: readonly ChartIndicatorPoint[],
  histogram: boolean,
  chartTimes: ReadonlyMap<number, number>,
  referenceLevels: readonly number[],
): IndicatorSeriesPlan {
  return {
    id,
    pane: result.panel,
    type: histogram ? 'histogram' : 'line',
    color: result.color,
    points: points.flatMap((point) => {
      const time = chartTimes.get(point.t);
      return point.value === null || time === undefined ? [] : [{ time, value: point.value }];
    }),
    referenceLevels,
  };
}

function indicatorLabel(entry: ChartIndicatorEntry): string {
  const values = Object.values(entry.params).join('/');
  return values ? `${entry.name.toUpperCase()} ${values}` : entry.name.toUpperCase();
}
