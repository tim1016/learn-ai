/**
 * Wire-matching parameter range types + a client-side pre-launch run-count
 * estimate. Mirrors PythonDataService/app/research/recency/grid.py's
 * ValueListRange / LowHighStepRange / expand_grid size math (design spec
 * D4, D11) — the server is the actual source of truth (and rejects a
 * pathological grid past its own sanity ceiling); this is a fast,
 * client-only preview so the run count updates as the user edits ranges.
 */

import type { ParamProperty, StrategyInfo } from "../../../strategy-lab/strategy-lab.models";

export interface ValueListRange {
  type: "value_list";
  values: number[];
}

export interface LowHighStepRange {
  type: "low_high_step";
  low: number;
  high: number;
  step: number;
}

export type ParamRange = ValueListRange | LowHighStepRange;

export function numericStrategyParams(strategy: StrategyInfo): [string, ParamProperty][] {
  const properties: Record<string, ParamProperty> = strategy.params_schema.properties ?? {};
  return Object.entries(properties).filter(
    ([name, property]) => name !== "symbol" && (property.type === "number" || property.type === "integer"),
  );
}

export function defaultNumericValue(property: ParamProperty): number {
  return typeof property.default === "number" ? property.default : 0;
}

export function defaultRangeForParameter(property: ParamProperty): ParamRange {
  return { type: "value_list", values: [defaultNumericValue(property)] };
}

export interface StrategyRangeConfig {
  strategyKey: string;
  paramRanges: Record<string, ParamRange>;
}

function rangeSize(range: ParamRange): number {
  if (range.type === "value_list") {
    return range.values.length;
  }
  if (range.step <= 0 || range.low > range.high) return 0;
  return Math.round((range.high - range.low) / range.step) + 1;
}

function comboCount(strategy: StrategyRangeConfig): number {
  const ranges = Object.values(strategy.paramRanges);
  if (ranges.length === 0) return 1;
  return ranges.reduce((product, range) => product * rangeSize(range), 1);
}

export function computeGridSize(strategies: StrategyRangeConfig[], symbols: string[]): number {
  if (strategies.length === 0 || symbols.length === 0) return 0;
  const combosPerSymbol = strategies.reduce((sum, s) => sum + comboCount(s), 0);
  return symbols.length * combosPerSymbol;
}
