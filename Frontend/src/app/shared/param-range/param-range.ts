/**
 * Wire-matching parameter range types + a client-side pre-launch run-count
 * estimate. Mirrors PythonDataService/app/research/sweep/grid.py's
 * ValueListRange / LowHighStepRange / expand_grid size math (Recency Chart
 * design spec D4, D11; Grid Search PRD #1926) — the server is the actual
 * source of truth (and rejects a pathological grid past its own sanity
 * ceiling); this is a fast, client-only preview so the count updates as the
 * user edits ranges. Shared by the Recency Chart, Grid Search, and
 * Walk-Forward forms.
 */

import type { ParamProperty, StrategyInfo } from "../../components/strategy-lab/strategy-lab.models";

export interface ValueListRange {
  type: "value_list";
  /**
   * Empty only while the range editor is refusing the text in its field
   * (#1940); every consumer asks `rangeProblem` before building a request,
   * and both request schemas reject an empty list (`min_length=1`) besides.
   */
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

/** A comma-separated value list as typed, or why it cannot become one (the first offending entry, named). */
export type ParsedValueList = { readonly values: number[] } | { readonly problem: string };

/**
 * Parse the value-list field's text. Nothing is dropped or repaired: an entry
 * that is not a finite number, an empty entry, or an empty list is reported
 * so the field can refuse it (#1940) — a silently shrunken grid would launch
 * an experiment other than the one the operator typed.
 */
export function parseValueList(raw: string): ParsedValueList {
  if (raw.trim() === "") return { problem: "Enter at least one value." };
  const values: number[] = [];
  for (const entry of raw.split(",").map((part) => part.trim())) {
    if (entry === "") return { problem: "Remove the empty entry next to a comma." };
    const value = Number(entry);
    if (!Number.isFinite(value)) return { problem: `"${entry}" is not a number.` };
    values.push(value);
  }
  return { values };
}

/**
 * Why a stored range cannot be launched, or null. The one place that reads
 * the refused-list encoding above; Grid Search, Walk-Forward and the Recency
 * timeline all ask this before they build a request.
 */
export function rangeProblem(range: ParamRange): string | null {
  return range.type === "value_list" && range.values.length === 0 ? "the value list needs fixing" : null;
}

/** Whether a stored range sweeps more than one value (a single-value list is a fixed setting). */
export function rangeVaries(range: ParamRange): boolean {
  return range.type === "low_high_step" || range.values.length > 1;
}

export interface StrategyRangeConfig {
  strategyKey: string;
  paramRanges: Record<string, ParamRange>;
}

function rangeSize(range: ParamRange): number {
  if (range.type === "value_list") {
    return range.values.length;
  }
  if (![range.low, range.high, range.step].every(Number.isFinite)) return 0;
  if (range.step <= 0 || range.low > range.high) return 0;
  return exactRangeCount(range.low, range.high, range.step);
}

/**
 * `floor((high - low) / step) + 1` on the exact decimals each number's
 * shortest repr denotes — the same count the Python side computes with
 * exact rationals, so the preview never shows a cell the launch will not
 * run (e.g. 0..0.29999999999999993 step 0.1 is three cells, not four).
 */
function exactRangeCount(low: number, high: number, step: number): number {
  const [l, h, s] = [low, high, step].map(decimalParts);
  const scale = Math.max(l.decimals, h.decimals, s.decimals);
  const scaled = (part: DecimalParts): bigint => part.digits * 10n ** BigInt(scale - part.decimals);
  return Number((scaled(h) - scaled(l)) / scaled(s)) + 1;
}

interface DecimalParts {
  /** All significant digits as an integer, sign included. */
  digits: bigint;
  /** How many of those digits sit right of the decimal point (may be negative for large exponents). */
  decimals: number;
}

/** Decompose a finite number's shortest repr ("0.15", "1e-7", "1.5e+21") into an exact decimal. */
function decimalParts(value: number): DecimalParts {
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:e([+-]?\d+))?$/i.exec(value.toString());
  if (match === null) throw new Error(`not a finite decimal: ${value}`);
  const [, sign, whole, fraction = "", exponent = "0"] = match;
  const decimals = fraction.length - Number(exponent);
  const digits = BigInt(`${sign}${whole}${fraction}`);
  return decimals >= 0 ? { digits, decimals } : { digits: digits * 10n ** BigInt(-decimals), decimals: 0 };
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
