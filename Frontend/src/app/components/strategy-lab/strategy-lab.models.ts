import type { EngineValidationAnalytics } from "../lean-engine/engine-results/engine-validation-analytics.types";
import type { RunVerdict } from "../../api/run-verdict.types";
import type { DataPolicy } from "../../models/data-policy";
import type { BacktestRunDetail } from "../../graphql/backtest-runs.query";
import type { TickerRange } from "../../shared/ticker-range-picker";

export type EngineChoice = "python" | "lean" | "both";
export type EngineResolution = "minute" | "daily";
export type StrategyLabTab = "configuration" | "history";
export type LeanLauncherStatus = "unknown" | "checking" | "ready" | "blocked";
export type StrategyParameterValue =
  | string
  | number
  | boolean
  | null
  | StrategyParameterValue[]
  | { [key: string]: StrategyParameterValue };

export type StrategyLabRunPhase =
  | "idle"
  | "connecting"
  | "fetching_data"
  | "consolidating_bars"
  | "running_indicators"
  | "aggregating_results"
  | "persisting"
  | "loading_bars"
  | "simulating"
  | "computing_stats"
  | "completed"
  | "failed";

export interface StrategyInfo {
  name: string;
  display_name: string;
  description: string;
  params_schema: ParamsSchema;
  supported_resolutions: string[];
  algorithm_pseudocode?: string;
  gotchas?: string[];
  lean_twin?: string | null;
  strategy_bars: {
    timespan: "minute" | "day";
    multiplier: number;
    parameter?: string | null;
  };
  /** Recency Chart eligibility (design spec D1) — long-only equity, numeric-only params. */
  recency_supported?: boolean;
}

export interface ParamsSchema {
  title?: string;
  type?: string;
  properties?: Record<string, ParamProperty>;
  required?: string[];
}

export interface ParamProperty {
  type?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  description?: string;
  title?: string;
}

export interface EngineBacktestResponse {
  success: boolean;
  total_trades: number;
  net_profit: number;
  study_id?: number | null;
  validation_analytics?: EngineValidationAnalytics | null;
  error?: string;
}

/** Read-only parity evidence attached to one persisted Strategy Lab run. */
export interface StrategyLabParityView {
  status: string;
  createdAt?: number;
  reason: string | null;
  countsByCategory: { category: string; count: number }[];
  divergences: {
    category: string;
    message: string;
    trade_number?: number | null;
    ms_utc?: number | null;
  }[];
  nativeMetricParity?: {
    status: string;
    native_metric_count?: number;
    formatted_metric_count?: number;
    divergence_count?: number;
    source_commit?: string | null;
  } | null;
  readinessParity?: {
    status: string;
    compared_field_count?: number;
    mismatched_fields?: string[];
  } | null;
  inputParity?: {
    status: string;
    compared_field_count?: number;
    fixture_id?: string | null;
    fixture_sha256?: string | null;
    mismatched_fields?: string[];
  } | null;
}

export interface StrategyLabConfiguration {
  engine: EngineChoice;
  range: TickerRange;
  parameters: Record<string, unknown>;
  fillMode: "signal_bar_close" | "next_bar_open";
  initialCash: number;
  commissionPerOrder: number;
  dataPolicy: DataPolicy | null;
}

export interface ParsedRunVerdict {
  verdict: RunVerdict | null;
  error: string | null;
}

export function parseRunVerdict(value: string | null): RunVerdict | null {
  return parseRunVerdictEnvelope(value).verdict;
}

export function parseRunVerdictEnvelope(value: string | null): ParsedRunVerdict {
  if (!value) return { verdict: null, error: null };
  try {
    const parsed: unknown = JSON.parse(value);
    return isRunVerdict(parsed)
      ? { verdict: parsed, error: null }
      : { verdict: null, error: "Persisted verdict data is incomplete or malformed." };
  } catch {
    return { verdict: null, error: "Persisted verdict data is malformed." };
  }
}

export function toStrategyLabConfiguration(
  run: BacktestRunDetail,
  currentRange: TickerRange,
): StrategyLabConfiguration {
  const parameters = parseStrategyParameters(run.parameters);
  const symbol = run.dataPolicy?.symbol ?? run.symbol ?? readString(parameters, "symbol") ?? "SPY";
  const policy = run.dataPolicy;
  const timespan = policy?.input_bars.timespan;
  return {
    engine: run.requestedEngine ?? inferRequestedEngine(run),
    range: {
      ...currentRange,
      symbol: symbol.toUpperCase(),
      from: run.startDate,
      to: run.endDate,
      resolution: timespan === "day" ? "daily" : timespan ?? "minute",
      multiplier: policy?.input_bars.multiplier ?? 1,
      session: policy?.session === "extended" ? "extended" : "rth",
      autoFetch: policy?.provider_kind !== "fixture",
    },
    parameters: { ...parameters, symbol: symbol.toUpperCase() },
    fillMode: run.fillMode === "next_bar_open" ? "next_bar_open" : "signal_bar_close",
    initialCash: run.initialCash,
    commissionPerOrder: run.commissionPerOrder ?? 0,
    dataPolicy: policy ?? null,
  };
}

export function parseStrategyParameters(value: string | null): Record<string, StrategyParameterValue> {
  if (!value) return {};
  try {
    const parsed: unknown = JSON.parse(value);
    if (!isParameterRecord(parsed)) {
      throw new Error("the decoded value is not a finite JSON object");
    }
    const { starting_cash: _legacyCash, ...parameters } = parsed;
    return parameters;
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown parse failure";
    throw new Error(`Saved run parameters are malformed: ${detail}`, { cause: error });
  }
}

export function previousIsoDate(isoDate: string): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  const previous = new Date(Date.UTC(year, month - 1, day - 1));
  return [
    previous.getUTCFullYear(),
    String(previous.getUTCMonth() + 1).padStart(2, "0"),
    String(previous.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

function isParameterRecord(value: unknown): value is Record<string, StrategyParameterValue> {
  return value !== null && typeof value === "object" && !Array.isArray(value) &&
    Object.values(value).every(isParameterValue);
}

function readString(value: Record<string, unknown>, key: string): string | null {
  const candidate = value[key];
  return typeof candidate === "string" && candidate.trim() ? candidate : null;
}

function inferRequestedEngine(run: BacktestRunDetail): EngineChoice {
  return run.engine === "LEAN" || run.source === "lean-sidecar" ? "lean" : "python";
}

function isParameterValue(value: unknown): value is StrategyParameterValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isParameterValue);
  return isParameterRecord(value);
}

function isRunVerdict(value: unknown): value is RunVerdict {
  if (!isRecord(value)) return false;
  return (
    typeof value["verdict_version"] === "number" &&
    typeof value["engine"] === "string" &&
    typeof value["generated_at_ms"] === "number" &&
    isNullableNumber(value["composite"]) &&
    isNullableString(value["grade"]) &&
    isNullableString(value["signal"]) &&
    typeof value["headline"] === "string" &&
    isStringArray(value["red_flags"]) &&
    Array.isArray(value["dimensions"]) && value["dimensions"].every(isRunVerdictDimension) &&
    isStringArray(value["missing_metrics"]) &&
    typeof value["normalized_weights"] === "boolean" &&
    (value["cleanliness"] === null || isRecord(value["cleanliness"]))
  );
}

function isRunVerdictDimension(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value["key"] === "string" &&
    typeof value["label"] === "string" &&
    typeof value["weight"] === "number" &&
    isNullableNumber(value["score"]) &&
    typeof value["summary"] === "string" &&
    Array.isArray(value["sub_scores"]) && value["sub_scores"].every(isRunVerdictSubScore)
  );
}

function isRunVerdictSubScore(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value["key"] === "string" &&
    typeof value["label"] === "string" &&
    isNullableNumber(value["score"]) &&
    isNullableNumber(value["raw_value"]) &&
    typeof value["display"] === "string" &&
    typeof value["note"] === "string"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNullableNumber(value: unknown): boolean {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string";
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}
