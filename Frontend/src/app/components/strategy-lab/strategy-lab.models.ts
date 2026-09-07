import type { EngineValidationAnalytics } from "../lean-engine/engine-results/engine-validation-analytics.types";
import type { RunVerdict } from "../../api/run-verdict.types";
import type { DataPolicy } from "../../models/data-policy";
import type { BacktestRunDetail } from "../../services/backtest-runs.types";
import { runWindowDate } from "../../services/backtest-runs.types";
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
  /** Registry role: `production_candidate` or `operational_validation_harness`. */
  strategy_category?: string;
  /** Structured sweep eligibility shared by Recency Chart, Grid Search, and Walk-Forward (PRD #1926).
   *  Always present on the wire; optional here only so fixtures predating it still type-check. A
   *  consumer treats absence as ineligible, never as a fallback to another flag. */
  sweep_eligibility?: SweepEligibility;
}

export interface SweepEligibility {
  eligible: boolean;
  reason_codes: string[];
  offending_parameters: string[];
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

/** What the rail shows for a run: its configuration and the strategy it belongs to. */
export interface StrategyLabRunInputs {
  strategyName: string;
  engine: EngineChoice;
  range: TickerRange;
  parameters: Record<string, unknown>;
  fillMode: "signal_bar_close" | "next_bar_open";
  initialCash: number;
  commissionPerOrder: number;
  dataPolicy: DataPolicy | null;
}

/** What a saved run and a submitted job payload each state about a run, before the rail's shape is composed from it. */
interface RunFacts {
  strategyName: string;
  engine: EngineChoice;
  symbol: string;
  from: string;
  to: string;
  parameters: Record<string, StrategyParameterValue>;
  fillMode: unknown;
  initialCash: number;
  commissionPerOrder: number | null | undefined;
  policy: DataPolicy | null;
  autoFetch: boolean;
}

/** The one derivation of the rail's inputs, so a saved run and an in-flight job cannot drift apart. */
function runInputsFrom(facts: RunFacts, currentRange: TickerRange): StrategyLabRunInputs {
  const symbol = facts.symbol.toUpperCase();
  const timespan = facts.policy?.input_bars.timespan;
  return {
    strategyName: facts.strategyName,
    engine: facts.engine,
    range: {
      ...currentRange,
      symbol,
      from: facts.from,
      to: facts.to,
      resolution: timespan === "day" ? "daily" : timespan ?? "minute",
      multiplier: facts.policy?.input_bars.multiplier ?? 1,
      session: facts.policy?.session === "extended" ? "extended" : "rth",
      autoFetch: facts.autoFetch,
    },
    parameters: { ...facts.parameters, symbol },
    fillMode: facts.fillMode === "next_bar_open" ? "next_bar_open" : "signal_bar_close",
    initialCash: facts.initialCash,
    commissionPerOrder: facts.commissionPerOrder ?? 0,
    dataPolicy: facts.policy,
  };
}

/** The inputs a saved run was produced with. Throws when its persisted parameters are malformed. */
export function inputsFromSavedRun(run: BacktestRunDetail, currentRange: TickerRange): StrategyLabRunInputs {
  const parameters = parseStrategyParameters(run.parameters);
  const policy = run.dataPolicy ?? null;
  return runInputsFrom({
    strategyName: run.strategyName,
    engine: run.requestedEngine ?? inferRequestedEngine(run),
    symbol: policy?.symbol ?? run.symbol ?? readString(parameters, "symbol") ?? "SPY",
    from: runWindowDate(run.startDate),
    to: runWindowDate(run.endDate),
    parameters,
    fillMode: run.fillMode,
    initialCash: run.initialCash,
    commissionPerOrder: run.commissionPerOrder,
    policy,
    autoFetch: policy?.provider_kind !== "fixture",
  }, currentRange);
}

/**
 * The inputs an in-flight `engine_backtest` job was started with, read back
 * from the payload `StrategyLabRunner.runPython` submitted (#1953). `null`
 * for anything but a complete payload of that shape — the rail is then left
 * alone rather than describing a guess. LEAN-only jobs are deliberately not
 * read: their request carries the window as session-open instants with an
 * exclusive end, and only the trading calendar can turn that back into the
 * end date the operator chose; the persisted run reconciles the rail once
 * the job completes.
 */
export function inputsFromBacktestJob(
  jobParameters: Readonly<Record<string, unknown>> | undefined,
  currentRange: TickerRange,
): StrategyLabRunInputs | null {
  const backtest = jobParameters?.["backtest"];
  if (!isRecord(backtest)) return null;
  const strategyName = readString(backtest, "strategy_name");
  const engine = backtest["requested_engine"];
  const from = readIsoDate(backtest, "start_date");
  const to = readIsoDate(backtest, "end_date");
  const initialCash = backtest["initial_cash"];
  const policy = backtest["data_policy"];
  const parameters = backtest["params"];
  if (
    strategyName === null || !isEngineChoice(engine) || from === null || to === null ||
    typeof initialCash !== "number" || !isDataPolicyPayload(policy) || !isParameterRecord(parameters)
  ) {
    return null;
  }
  const commission = backtest["commission_per_order"];
  return runInputsFrom({
    strategyName,
    engine,
    symbol: policy.symbol,
    from,
    to,
    parameters,
    fillMode: backtest["fill_mode"],
    initialCash,
    commissionPerOrder: typeof commission === "number" ? commission : null,
    policy,
    autoFetch: backtest["auto_fetch"] === true,
  }, currentRange);
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

function isEngineChoice(value: unknown): value is EngineChoice {
  return value === "python" || value === "lean" || value === "both";
}

/** A calendar date as `YYYY-MM-DD`, the shape `TickerRange.from`/`to` hold. */
function readIsoDate(value: Record<string, unknown>, key: string): string | null {
  const candidate = readString(value, key);
  if (candidate === null || !/^\d{4}-\d{2}-\d{2}$/.test(candidate)) return null;
  const [year, month, day] = candidate.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  const exists = parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
  return exists ? candidate : null;
}

function isBarsSpec(value: unknown): value is DataPolicy["input_bars"] {
  if (!isRecord(value)) return false;
  const { timespan, multiplier } = value;
  return (timespan === "minute" || timespan === "hour" || timespan === "day") &&
    typeof multiplier === "number" && Number.isInteger(multiplier) && multiplier > 0;
}

/** The complete policy `StrategyLabConfigStore.composeDataPolicy` submits, as it comes back from a job snapshot. */
function isDataPolicyPayload(value: unknown): value is DataPolicy {
  if (!isRecord(value)) return false;
  return (
    (value["source"] === "polygon" || value["source"] === "synthetic") &&
    typeof value["symbol"] === "string" &&
    typeof value["adjusted"] === "boolean" &&
    (value["session"] === "regular" || value["session"] === "extended") &&
    isBarsSpec(value["input_bars"]) &&
    isBarsSpec(value["strategy_bars"]) &&
    value["timestamp_policy"] === "bar_close_ms_utc" &&
    value["timezone"] === "America/New_York" &&
    (value["provider_kind"] === "live" || value["provider_kind"] === "fixture") &&
    isNullableString(value["fixture_id"]) &&
    isNullableString(value["fixture_sha256"])
  );
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
