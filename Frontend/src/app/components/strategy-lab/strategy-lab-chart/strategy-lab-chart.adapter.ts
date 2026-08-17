import {
  CHART_INDICATOR_SERIES_COLORS,
  type ChartIndicatorPoint,
  type ChartIndicatorResult,
  type TradingPoint,
  type TradingSeries,
  type TradingSubPane,
} from "../../../shared/trading-chart";

export const SERIES_COLORS = CHART_INDICATOR_SERIES_COLORS;

export interface ChartIndicatorSpec {
  id: string;
  name: string;
  label: string;
}

export function indicatorMatches(
  leftName: string,
  leftParams: Record<string, number>,
  rightName: string,
  rightParams: Record<string, number>,
): boolean {
  if (leftName !== rightName) return false;
  const leftKeys = Object.keys(leftParams).sort();
  const rightKeys = Object.keys(rightParams).sort();
  return leftKeys.length === rightKeys.length &&
    leftKeys.every((key, index) => key === rightKeys[index] && leftParams[key] === rightParams[key]);
}

export function timeframeKey(timespan: string, multiplier: number): string {
  if (timespan === "day") return `${multiplier}D`;
  if (timespan === "hour") return `${multiplier}h`;
  return `${multiplier}m`;
}

export function normalizeIndicatorResults(
  results: readonly ChartIndicatorResult[],
  specs: readonly ChartIndicatorSpec[] = [],
): {
  overlays: TradingSeries[];
  subPanes: TradingSubPane[];
} {
  const overlays: TradingSeries[] = [];
  const paneMap = new Map<string, TradingSubPane>();
  const specsById = new Map(specs.map((spec) => [spec.id, spec]));
  results.forEach((result, resultIndex) => {
    const spec = specsById.get(result.id);
    const series = normalizeResultSeries(result, resultIndex, spec);
    const panel = resolvePanel(result, spec);
    if (panel.id === "main") {
      overlays.push(...series);
      return;
    }
    const pane = paneMap.get(panel.id) ?? {
      id: panel.id,
      label: panel.label,
      series: [],
      referenceLevels: [],
    };
    pane.series.push(...series);
    pane.referenceLevels = [...new Set([...(pane.referenceLevels ?? []), ...(result.refs ?? [])])];
    paneMap.set(panel.id, pane);
  });
  return { overlays, subPanes: [...paneMap.values()] };
}

function resolvePanel(
  result: ChartIndicatorResult,
  spec: ChartIndicatorSpec | undefined,
): { id: string; label: string } {
  // EMA is evidence in its own right. Keeping it off price candles makes
  // crossovers readable without altering any producer-owned values.
  if (spec?.name.toLowerCase() === "ema") return { id: "ema", label: "EMA" };
  return { id: result.panel, label: result.panel.toUpperCase() };
}

function normalizeResultSeries(
  result: ChartIndicatorResult,
  resultIndex: number,
  spec: ChartIndicatorSpec | undefined,
): TradingSeries[] {
  const color = result.color || SERIES_COLORS[resultIndex % SERIES_COLORS.length];
  if (Array.isArray(result.data)) {
    return [{
      id: result.id,
      name: spec?.label ?? result.id,
      color,
      type: result.type === "histogram" ? "histogram" : "line",
      points: toTradingPoints(result.data),
    }];
  }
  return Object.entries(result.data).map(([name, points], index) => ({
    id: `${result.id}-${name}`,
    name: `${spec?.label ?? result.id} · ${name}`,
    color: index === 0 ? color : SERIES_COLORS[(resultIndex + index) % SERIES_COLORS.length],
    type: name.toLowerCase().includes("hist") ? "histogram" : "line",
    points: toTradingPoints(points),
  }));
}

function toTradingPoints(points: readonly ChartIndicatorPoint[]): TradingPoint[] {
  return points
    .filter((point): point is ChartIndicatorPoint & { value: number } => point.value !== null)
    .map((point) => ({ timeMs: point.t, value: point.value }));
}
