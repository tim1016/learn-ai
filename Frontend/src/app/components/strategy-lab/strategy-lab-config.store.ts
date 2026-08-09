import { HttpClient } from "@angular/common/http";
import { computed, effect, inject, Injectable, signal } from "@angular/core";
import { toSignal } from "@angular/core/rxjs-interop";
import { ActivatedRoute, type ParamMap } from "@angular/router";
import { firstValueFrom, map } from "rxjs";

import { environment } from "../../../environments/environment";
import { toDataPolicyPayload, type DataPolicy } from "../../models/data-policy";
import { toMostRecentWeekday } from "../../shared/date/weekday";
import { TICKER_POOL, RECENT_TICKERS } from "../../shared/ticker-catalog";
import type { TickerRange } from "../../shared/ticker-range-picker";
import {
  leanValidationTemplateForStrategy,
  leanValidationTemplateLabel,
  type LeanValidationTemplate,
} from "../lean-engine/lean-validation-template";
import type {
  EngineChoice,
  EngineResolution,
  StrategyInfo,
  StrategyLabTab,
} from "./strategy-lab.models";

interface EngineLaunchParams {
  strategy?: string;
  engine?: EngineChoice;
  symbol?: string;
  from?: string;
  to?: string;
  resolution?: EngineResolution;
  tab?: StrategyLabTab;
}

const CONFIG_NAV_KEY = "engineLab.configNavOverride";

@Injectable()
export class StrategyLabConfigStore {
  private readonly http = inject(HttpClient);
  private readonly launchParams = toSignal(
    inject(ActivatedRoute).queryParamMap.pipe(map(parseEngineLaunchParams)),
    { initialValue: {} },
  );
  private readonly appliedLaunchParamsKey = signal<string | null>(null);
  private readonly retainingHistoricalSelection = signal(false);
  private readonly configNavOverride = signal<"expanded" | "collapsed" | null>(loadNavOverride());

  readonly engine = signal<EngineChoice>("python");
  readonly strategies = signal<StrategyInfo[]>([]);
  readonly strategiesLoading = signal(false);
  readonly strategiesError = signal<string | null>(null);
  readonly selectedStrategyName = signal<string | null>(null);
  readonly paramValues = signal<Record<string, unknown>>({});
  readonly range = signal<TickerRange>({
    symbol: "SPY",
    from: defaultStart(),
    to: defaultEnd(),
    resolution: "minute",
    autoFetch: true,
  });
  readonly fillMode = signal<"signal_bar_close" | "next_bar_open">("signal_bar_close");
  readonly initialCash = signal(100000);
  readonly commissionPerOrder = signal(1);
  readonly activeTab = signal<StrategyLabTab>("configuration");
  readonly configNavCollapsed = signal(loadNavOverride() === "collapsed");
  readonly configurationWarning = signal<string | null>(null);
  readonly restoredDataPolicy = signal<DataPolicy | null>(null);

  readonly tickerPool = TICKER_POOL;
  readonly recentTickers = RECENT_TICKERS;
  readonly startDate = computed(() => this.range().from);
  readonly endDate = computed(() => this.range().to);
  readonly resolution = computed<EngineResolution>(() => {
    const resolution = this.range().resolution;
    return isEngineResolution(resolution) ? resolution : "minute";
  });
  readonly autoFetch = computed(() => this.range().autoFetch ?? false);
  readonly effectiveSymbol = computed(() => {
    const schemaSymbol = this.paramValues()["symbol"];
    return typeof schemaSymbol === "string" && schemaSymbol.trim()
      ? schemaSymbol.trim().toUpperCase()
      : this.range().symbol.trim().toUpperCase() || "SPY";
  });
  readonly availableStrategies = computed(() =>
    this.strategies().filter((strategy) =>
      (strategy.supported_resolutions ?? ["minute"]).includes(this.resolution()),
    ),
  );
  readonly selectableStrategies = computed(() => {
    const strategies = this.availableStrategies();
    return this.engine() === "python"
      ? strategies
      : strategies.filter(
          (strategy) =>
            leanValidationTemplateForStrategy(strategy.name, strategy.lean_twin) !== null,
        );
  });
  readonly selectedStrategy = computed(() => {
    const name = this.selectedStrategyName();
    return name ? this.strategies().find((strategy) => strategy.name === name) ?? null : null;
  });
  readonly rerunBlocked = computed(() => {
    const name = this.selectedStrategyName();
    return this.configurationWarning() !== null ||
      name === null ||
      !this.selectableStrategies().some((strategy) => strategy.name === name);
  });
  readonly leanValidationTemplate = computed<LeanValidationTemplate | null>(() => {
    const strategy = this.selectedStrategy();
    return strategy
      ? leanValidationTemplateForStrategy(strategy.name, strategy.lean_twin)
      : null;
  });
  readonly dataPolicy = computed(() => this.composeDataPolicy());
  readonly dataPolicyNote = computed(() => {
    const policy = this.dataPolicy();
    const input = barLabel(policy.input_bars);
    const strategy = barLabel(policy.strategy_bars);
    return input === strategy
      ? `Strategy evaluates ${strategy} bars.`
      : `Input ${input} bars are consolidated → strategy evaluates ${strategy} bars.`;
  });

  constructor() {
    effect(() => persistNavOverride(this.configNavOverride()));

    effect(() => {
      const current = this.range();
      const schema = this.selectedStrategy()?.params_schema;
      if (schema?.properties && "symbol" in schema.properties) {
        const params = this.paramValues();
        if (params["symbol"] !== current.symbol) {
          this.paramValues.set({ ...params, symbol: current.symbol.toUpperCase() });
        }
      }
    });

    effect(() => {
      const available = this.selectableStrategies();
      const current = this.selectedStrategyName();
      if (available.length > 0 && (
        !current ||
        (!this.retainingHistoricalSelection() && !available.some((strategy) => strategy.name === current))
      )) {
        this.selectStrategy(available[0].name);
      }
    });

    effect(() => {
      const params = this.launchParams();
      const key = launchParamsKey(params);
      if (key === this.appliedLaunchParamsKey() || key === "||||||") return;
      if (this.strategies().length === 0) return;
      this.applyLaunchParams(params);
      this.appliedLaunchParamsKey.set(key);
    });
  }

  async loadStrategies(): Promise<void> {
    this.strategiesLoading.set(true);
    this.strategiesError.set(null);
    try {
      const list = await firstValueFrom(
        this.http.get<StrategyInfo[]>(`${environment.pythonServiceUrl}/api/engine/strategies`),
      );
      this.strategies.set(list);
      if (this.retainingHistoricalSelection()) this.reconcileHistoricalSelection();
      const first = list.find((strategy) =>
        (strategy.supported_resolutions ?? ["minute"]).includes(this.resolution()),
      );
      if (first && this.selectedStrategyName() === null) this.selectStrategy(first.name);
    } catch (error) {
      this.strategiesError.set(error instanceof Error ? error.message : "Failed to load strategies");
    } finally {
      this.strategiesLoading.set(false);
    }
  }

  selectStrategy(name: string): void {
    this.retainingHistoricalSelection.set(false);
    this.applyStrategy(name);
  }

  restoreStrategy(name: string): void {
    this.retainingHistoricalSelection.set(true);
    this.applyStrategy(name);
  }

  private applyStrategy(name: string): void {
    this.restoredDataPolicy.set(null);
    this.selectedStrategyName.set(name);
    const schema = this.strategies().find((strategy) => strategy.name === name)?.params_schema;
    const defaults: Record<string, unknown> = {};
    for (const [field, property] of Object.entries(schema?.properties ?? {})) {
      if (property.default !== undefined) defaults[field] = property.default;
    }
    this.paramValues.set(defaults);
    this.configurationWarning.set(null);
  }

  updateParameter(field: string, rawValue: string, type: string | undefined): void {
    const next = { ...this.paramValues() };
    if (type === "integer") {
      const parsed = Number.parseInt(rawValue, 10);
      next[field] = Number.isNaN(parsed) ? undefined : parsed;
    } else if (type === "number") {
      const parsed = Number.parseFloat(rawValue);
      next[field] = Number.isNaN(parsed) ? undefined : parsed;
    } else {
      next[field] = rawValue;
    }
    this.paramValues.set(next);
    const value = next[field];
    const cadenceParameter = this.selectedStrategy()?.strategy_bars.parameter;
    if (field === cadenceParameter && typeof value === "number" && Number.isInteger(value) && value > 0) {
      this.restoredDataPolicy.update((policy) => policy === null ? null : {
        ...policy,
        strategy_bars: { ...policy.strategy_bars, multiplier: value },
      });
    }
  }

  composeDataPolicy(): DataPolicy {
    const restored = this.restoredDataPolicy();
    if (restored !== null) return restored;
    const timespan: DataPolicy["input_bars"]["timespan"] =
      this.resolution() === "daily" ? "day" : "minute";
    const declared = this.selectedStrategy()?.strategy_bars;
    const configuredMultiplier = declared?.parameter
      ? this.paramValues()[declared.parameter]
      : declared?.multiplier;
    const multiplier =
      typeof configuredMultiplier === "number" && Number.isInteger(configuredMultiplier) && configuredMultiplier > 0
        ? configuredMultiplier
        : declared?.multiplier ?? 1;
    const strategyBars = {
      timespan: declared?.timespan ?? timespan,
      multiplier,
    };
    return {
      source: "polygon",
      symbol: this.effectiveSymbol(),
      adjusted: this.engine() !== "both",
      session: "regular",
      input_bars: { timespan, multiplier: 1 },
      strategy_bars: strategyBars,
      timestamp_policy: "bar_close_ms_utc",
      timezone: "America/New_York",
      provider_kind: "live",
      fixture_id: null,
      fixture_sha256: null,
    };
  }

  changeEngine(engine: EngineChoice): void {
    this.retainingHistoricalSelection.set(false);
    this.configurationWarning.set(null);
    this.engine.set(engine);
    this.restoredDataPolicy.update((policy) => policy === null ? null : {
      ...policy,
      adjusted: engine !== "both",
    });
    if (this.restoredDataPolicy() !== null) this.reconcileHistoricalSelection();
  }

  changeRange(range: TickerRange): void {
    const previous = this.range();
    this.retainingHistoricalSelection.set(false);
    this.configurationWarning.set(null);
    this.range.set(range);
    this.restoredDataPolicy.update((policy) => {
      if (policy === null) return null;
      let next = policy;
      if (range.symbol !== previous.symbol) {
        next = { ...next, symbol: range.symbol.trim().toUpperCase() };
      }
      if (range.resolution !== previous.resolution || range.multiplier !== previous.multiplier) {
        next = {
          ...next,
          input_bars: {
            timespan: range.resolution === "daily" ? "day" : "minute",
            multiplier: validMultiplier(range.multiplier, 1),
          },
        };
      }
      if (range.session !== previous.session) {
        next = { ...next, session: range.session === "extended" ? "extended" : "regular" };
      }
      if (range.autoFetch !== previous.autoFetch) {
        next = range.autoFetch === false
          ? { ...next, provider_kind: "fixture" }
          : { ...next, provider_kind: "live", fixture_id: null, fixture_sha256: null };
      }
      return next;
    });
    if (this.restoredDataPolicy() !== null) this.reconcileHistoricalSelection();
  }

  restoreDataPolicy(policy: DataPolicy | null): void {
    this.restoredDataPolicy.set(policy === null ? null : toDataPolicyPayload(policy));
    this.reconcileHistoricalSelection();
  }

  private reconcileHistoricalSelection(): void {
    const selectedName = this.selectedStrategyName();
    const selected = selectedName === null
      ? null
      : this.strategies().find((strategy) => strategy.name === selectedName) ?? null;
    if (selected !== null) {
      const defaults: Record<string, unknown> = {};
      for (const [field, property] of Object.entries(selected.params_schema.properties ?? {})) {
        if (property.default !== undefined) defaults[field] = property.default;
      }
      this.paramValues.set({ ...defaults, ...this.paramValues() });
    }
    const selectedIsRunnable = selectedName !== null &&
      this.selectableStrategies().some((strategy) => strategy.name === selectedName);
    const policy = this.restoredDataPolicy();
    this.configurationWarning.set(
      policy?.input_bars.timespan === "hour"
        ? "This saved run used hourly input bars, which Strategy Lab cannot rerun without changing its data policy. The persisted report remains available."
        : selectedIsRunnable
          ? null
          : "This saved run uses a retired or unsupported strategy. Its persisted report remains available, but it cannot be rerun unchanged.",
    );
  }

  leanTemplateLabel(): string | null {
    const template = this.leanValidationTemplate();
    return template ? leanValidationTemplateLabel(template) : null;
  }

  toggleConfigNav(): void {
    const collapsed = !this.configNavCollapsed();
    this.configNavCollapsed.set(collapsed);
    this.configNavOverride.set(collapsed ? "collapsed" : "expanded");
  }

  private applyLaunchParams(params: EngineLaunchParams): void {
    if (params.engine) this.engine.set(params.engine);
    const strategy = params.strategy ? this.findStrategy(params.strategy) : null;
    const resolution = this.resolveResolution(strategy, params.resolution);
    this.range.update((range) => ({
      ...range,
      symbol: params.symbol?.toUpperCase() ?? range.symbol,
      from: params.from ?? range.from,
      to: params.to ?? range.to,
      resolution,
    }));

    if (strategy) {
      this.selectStrategy(strategy.name);
      const supported = (strategy.supported_resolutions ?? ["minute"]).filter(isEngineResolution);
      if (params.resolution && !supported.includes(params.resolution)) {
        this.configurationWarning.set(
          `${strategy.display_name} does not support ${params.resolution} data. Using ${resolution} instead.`,
        );
      }
    } else if (params.strategy) {
      this.configurationWarning.set(`Strategy "${params.strategy}" is not registered in Strategy Lab.`);
    }
    this.activeTab.set(params.tab ?? "configuration");
  }

  private findStrategy(key: string): StrategyInfo | null {
    const normalized = normalizeStrategyKey(key);
    return (
      this.strategies().find(
        (strategy) =>
          strategy.name === key ||
          strategy.name.toLowerCase() === key.toLowerCase() ||
          normalizeStrategyKey(strategy.name) === normalized,
      ) ?? null
    );
  }

  private resolveResolution(
    strategy: StrategyInfo | null,
    requested: EngineResolution | undefined,
  ): EngineResolution {
    if (!strategy) return requested ?? this.resolution();
    const supported = (strategy.supported_resolutions ?? ["minute"]).filter(isEngineResolution);
    return requested && supported.includes(requested) ? requested : supported[0] ?? "minute";
  }
}

function parseEngineLaunchParams(params: ParamMap): EngineLaunchParams {
  const tab = params.get("tab");
  return {
    strategy: nonBlank(params.get("strategy")),
    engine: parseEngineChoice(params.get("engine")),
    symbol: nonBlank(params.get("symbol")),
    from: parseIsoDate(params.get("from")),
    to: parseIsoDate(params.get("to")),
    resolution: parseResolution(params.get("resolution")),
    tab: tab === "history" ? "history" : tab === "configuration" ? "configuration" : undefined,
  };
}

function validMultiplier(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : fallback;
}

function parseEngineChoice(value: string | null): EngineChoice | undefined {
  return value === "python" || value === "lean" || value === "both" ? value : undefined;
}

function parseResolution(value: string | null): EngineResolution | undefined {
  return isEngineResolution(value) ? value : undefined;
}

function isEngineResolution(value: unknown): value is EngineResolution {
  return value === "minute" || value === "daily";
}

function parseIsoDate(value: string | null): string | undefined {
  return value !== null && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : undefined;
}

function nonBlank(value: string | null): string | undefined {
  return value?.trim() || undefined;
}

function launchParamsKey(params: EngineLaunchParams): string {
  return [
    params.strategy ?? "",
    params.engine ?? "",
    params.symbol ?? "",
    params.from ?? "",
    params.to ?? "",
    params.resolution ?? "",
    params.tab ?? "",
  ].join("|");
}

function normalizeStrategyKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function barLabel(bars: { timespan: string; multiplier: number }): string {
  return `${bars.multiplier}-${bars.timespan === "day" ? "day" : "minute"}`;
}

function yesterday(): Date {
  const date = new Date();
  date.setDate(date.getDate() - 1);
  date.setHours(0, 0, 0, 0);
  return date;
}

function toIso(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function defaultEnd(): string {
  return toIso(toMostRecentWeekday(yesterday()));
}

function defaultStart(): string {
  const start = yesterday();
  start.setDate(start.getDate() - 30);
  return toIso(toMostRecentWeekday(start));
}

function loadNavOverride(): "expanded" | "collapsed" | null {
  try {
    if (typeof localStorage === "undefined") return null;
    const value = localStorage.getItem(CONFIG_NAV_KEY);
    return value === "expanded" || value === "collapsed" ? value : null;
  } catch {
    return null;
  }
}

function persistNavOverride(value: "expanded" | "collapsed" | null): void {
  try {
    if (typeof localStorage === "undefined") return;
    if (value) localStorage.setItem(CONFIG_NAV_KEY, value);
    else localStorage.removeItem(CONFIG_NAV_KEY);
  } catch {
    // Browser storage is an optional preference, not execution state.
  }
}
