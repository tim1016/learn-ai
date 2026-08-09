import type { EngineValidationAnalytics } from "../lean-engine/engine-results/engine-validation-analytics.types";

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

function isParameterValue(value: unknown): value is StrategyParameterValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isParameterValue);
  return isParameterRecord(value);
}
