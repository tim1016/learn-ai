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
