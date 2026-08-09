import type {
  ChartIndicatorPoint,
  ChartIndicatorResult,
  TradingPoint,
  TradingSeries,
  TradingSubPane,
} from "../../../shared/trading-chart";

export const SERIES_COLORS = ["#ffb300", "#ff6d00", "#7aa9ff", "#ab47bc", "#26a69a", "#ec407a"];

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

export function normalizeIndicatorResults(results: readonly ChartIndicatorResult[]): {
  overlays: TradingSeries[];
  subPanes: TradingSubPane[];
} {
  const overlays: TradingSeries[] = [];
  const paneMap = new Map<string, TradingSubPane>();
  results.forEach((result, resultIndex) => {
    const series = normalizeResultSeries(result, resultIndex);
    if (result.panel === "main") {
      overlays.push(...series);
      return;
    }
    const pane = paneMap.get(result.panel) ?? {
      id: result.panel,
      label: result.panel.toUpperCase(),
      series: [],
      referenceLevels: [],
    };
    pane.series.push(...series);
    pane.referenceLevels = [...new Set([...(pane.referenceLevels ?? []), ...(result.refs ?? [])])];
    paneMap.set(result.panel, pane);
  });
  return { overlays, subPanes: [...paneMap.values()] };
}

function normalizeResultSeries(result: ChartIndicatorResult, resultIndex: number): TradingSeries[] {
  const color = result.color || SERIES_COLORS[resultIndex % SERIES_COLORS.length];
  if (Array.isArray(result.data)) {
    return [{
      id: result.id,
      name: result.id,
      color,
      type: result.type === "histogram" ? "histogram" : "line",
      points: toTradingPoints(result.data),
    }];
  }
  return Object.entries(result.data).map(([name, points], index) => ({
    id: `${result.id}-${name}`,
    name,
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
