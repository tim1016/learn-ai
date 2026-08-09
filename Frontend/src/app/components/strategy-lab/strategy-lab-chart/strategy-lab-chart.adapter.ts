import type {
  TradingPoint,
  TradingSeries,
  TradingSubPane,
} from "../../../shared/trading-chart";

export interface StrategyIndicatorRequest {
  id: string;
  name: string;
  params: Record<string, number>;
  label: string;
}

interface ChartIndicatorPoint {
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
}

export const SERIES_COLORS = ["#ffb300", "#ff6d00", "#7aa9ff", "#ab47bc", "#26a69a", "#ec407a"];

export function strategyIndicatorRequests(
  strategyName: string,
  parametersJson: string | null,
): StrategyIndicatorRequest[] {
  const params = parseParameters(parametersJson);
  const requests: StrategyIndicatorRequest[] = [];
  const add = (name: string, values: Record<string, number>) => requests.push({
    id: indicatorId(name, values),
    name,
    params: values,
    label: indicatorLabel(name, values),
  });

  const shortWindow = numberParam(params, "short_window");
  const longWindow = numberParam(params, "long_window");
  if (shortWindow && longWindow) {
    add("sma", { length: shortWindow });
    add("sma", { length: longWindow });
  }

  const fastEma = numberParam(params, "ema_fast_period");
  const slowEma = numberParam(params, "ema_slow_period");
  if (fastEma && slowEma) {
    add("ema", { length: fastEma });
    add("ema", { length: slowEma });
  }

  const rsiPeriod = numberParam(params, "rsi_period") ?? numberParam(params, "window");
  if (rsiPeriod) add("rsi", { length: rsiPeriod });

  if (requests.length === 0 && (strategyName === "ema_crossover_signal" || strategyName === "spy_ema_crossover")) {
    add("ema", { length: 5 });
    add("ema", { length: 10 });
    add("rsi", { length: 14 });
  }
  return requests;
}

export function indicatorId(name: string, params: Record<string, number>): string {
  const signature = Object.entries(params)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}-${value}`)
    .join("-");
  return signature ? `${name}-${signature}` : name;
}

export function indicatorLabel(name: string, params: Record<string, number>): string {
  const values = Object.values(params);
  return values.length === 0 ? name.toUpperCase() : `${name.toUpperCase()} ${values.join("/")}`;
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

function parseParameters(json: string | null): Record<string, unknown> {
  if (!json) return {};
  try {
    const parsed: unknown = JSON.parse(json);
    return typeof parsed === "object" && parsed !== null ? parsed as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function numberParam(params: Record<string, unknown>, key: string): number | null {
  const value = params[key];
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}
