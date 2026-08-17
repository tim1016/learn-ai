export interface ChartIndicatorPoint {
  t: number;
  value: number | null;
}

export interface ChartIndicatorResult {
  id: string;
  panel: string;
  type: string;
  color: string;
  data: ChartIndicatorPoint[] | Record<string, ChartIndicatorPoint[]>;
  refs?: number[];
  default_visible?: boolean | null;
}

export interface ChartIndicatorEntry {
  name: string;
  params: Record<string, number>;
}

export const CHART_INDICATOR_FALLBACK_COLOR = '#607D8B';
export const CHART_INDICATOR_SERIES_COLORS = [
  '#ffb300', '#ff6d00', '#7aa9ff', '#ab47bc', '#26a69a', '#ec407a',
] as const;
