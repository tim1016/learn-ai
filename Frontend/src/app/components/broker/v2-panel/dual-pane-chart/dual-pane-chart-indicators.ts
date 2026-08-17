import type { ChartBar } from '../lib/broker-v2-panel.types';
import type { components } from '../../../../api/broker.types';
import {
  CHART_INDICATOR_FALLBACK_COLOR,
  CHART_INDICATOR_SERIES_COLORS,
} from '../../../../shared/trading-chart';
import type {
  ChartIndicatorEntry,
  ChartIndicatorPoint,
  ChartIndicatorResult,
  TradingIndicatorChip,
} from '../../../../shared/trading-chart';

export interface SelectedChartIndicator extends ChartIndicatorEntry {
  id: string;
  label: string;
}

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

export function engineIndicatorId(entry: ChartIndicatorEntry): string {
  const values = Object.values(entry.params).join('_');
  return values ? `${entry.name}_${values}` : entry.name;
}

export function resultBelongsToIndicator(
  result: ChartIndicatorResult,
  entry: ChartIndicatorEntry,
): boolean {
  const engineId = engineIndicatorId(entry);
  return result.id === engineId || result.id.startsWith(`${engineId}_`);
}

export function toActiveIndicatorChips(
  selected: readonly SelectedChartIndicator[],
  results: readonly ChartIndicatorResult[],
): TradingIndicatorChip[] {
  return selected.map((indicator) => ({
    id: indicator.id,
    label: indicator.label,
    color: results.find((result) => resultBelongsToIndicator(result, indicator))?.color
      ?? CHART_INDICATOR_FALLBACK_COLOR,
  }));
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
        index === 0
          ? result.color
          : CHART_INDICATOR_SERIES_COLORS[index % CHART_INDICATOR_SERIES_COLORS.length],
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
  color = result.color,
): IndicatorSeriesPlan {
  return {
    id,
    pane: result.panel,
    type: histogram ? 'histogram' : 'line',
    color,
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
