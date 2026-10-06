import { HttpClient } from "@angular/common/http";
import { computed, effect, inject, Injectable, signal, untracked } from "@angular/core";
import { toSignal } from "@angular/core/rxjs-interop";
import { ActivatedRoute, type ParamMap } from "@angular/router";
import { firstValueFrom, map } from "rxjs";

import { environment } from "../../../environments/environment";
import { toDataPolicyPayload, type DataPolicy } from "../../models/data-policy";
import { RESEARCH_DEFAULT_FILL_MODE, type FillModeName } from "../../models/fill-mode";
import type { PriceAdjustmentMode } from "../../shared/data-lake";
import { ProviderBoundsService, TradingCalendarService, UNBOUNDED, offerQuickRanges, type DataGaps, type DateBounds } from "../../shared/date-picker";
import { HeldGapsService, TickerCatalogService, heldBoundsFor } from "../../shared/ticker-catalog";
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
  private readonly route = inject(ActivatedRoute);
  private readonly tickerCatalog = inject(TickerCatalogService);
  private readonly providerBounds = inject(ProviderBoundsService);
  private readonly heldGaps = inject(HeldGapsService);
  private readonly calendar = inject(TradingCalendarService);
  /** Whether the window has held both its dates yet; it is started once, never again after that. */
  private windowStarted = false;
  private readonly launchParams = toSignal(
    this.route.queryParamMap.pipe(map(parseEngineLaunchParams)),
    { initialValue: {} },
  );
  private readonly appliedLaunchParamsKey = signal<string | null>(null);
  private readonly retainingHistoricalSelection = signal(false);
  private readonly configNavOverride = signal<"expanded" | "collapsed" | null>(loadNavOverride());

  /**
   * The persisted run the page is displaying. Deliberately separate from
   * `launchParams`: run loading and launch-param application are two
   * mechanisms over one query string, and folding them together would make
   * every run load re-apply the launch params.
   */
  /** True once any launch parameters in the URL have been applied (or there are none),
   *  so a later application cannot reset inputs restored from a run or a job. Also true
   *  once the strategy list was read and holds no strategy: the parameters wait for one,
   *  so they are never applied, and a restore must not wait for them forever (#2872). */
  readonly launchParamsSettled = computed(() => {
    const key = launchParamsKey(this.launchParams());
    if (key === "||||||" || key === this.appliedLaunchParamsKey()) return true;
    return this.strategiesRead() && this.strategies().length === 0;
  });

  readonly activeRunParam = toSignal(
    this.route.queryParamMap.pipe(
      map((params) => parseRunId(params.get("run") ?? params.get("restoreRun"))),
    ),
    { initialValue: null },
  );

  readonly engine = signal<EngineChoice>("python");
  readonly strategies = signal<StrategyInfo[]>([]);
  readonly strategiesLoading = signal(false);
  /** Whether the strategy list has been read, whatever came back: a list, an empty one, or a failure. */
  private readonly strategiesRead = signal(false);
  readonly strategiesError = signal<string | null>(null);
  readonly selectedStrategyName = signal<string | null>(null);
  readonly paramValues = signal<Record<string, unknown>>({});
  readonly range = signal<TickerRange>({
    symbol: "SPY",
    // Empty until the calendar is read and the window starts on its last
    // month: only the trading calendar knows which days are sessions.
    from: "",
    to: "",
    resolution: "minute",
    autoFetch: true,
  });
  readonly fillMode = signal<FillModeName>(RESEARCH_DEFAULT_FILL_MODE);
  /** The fill a run on the selected engine uses: only a Python run takes the rail's; LEAN, alone or paired, fills at the signal bar's close. */
  readonly runFillMode = computed<FillModeName>(() => (this.engine() === "python" ? this.fillMode() : "signal_bar_close"));
  readonly initialCash = signal(100000);
  readonly commissionPerOrder = signal(0);
  readonly customLeanSource = signal<string | null>(null);
  readonly activeTab = signal<StrategyLabTab>("configuration");
  readonly configNavCollapsed = signal(loadNavOverride() === "collapsed");
  readonly configurationWarning = signal<string | null>(null);
  readonly restoredDataPolicy = signal<DataPolicy | null>(null);

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
  /**
   * Parameters the operator changed away from their schema default.
   *
   * `applyStrategy` materializes every default into `paramValues`, so
   * presence alone says nothing about intent — only a differing value does.
   * `symbol` is excluded: it reaches LEAN through the data policy rather
   * than through the algorithm's parameters.
   */
  readonly changedParameterNames = computed<readonly string[]>(() => {
    const properties = this.selectedStrategy()?.params_schema?.properties ?? {};
    const values = this.paramValues();
    return Object.entries(properties)
      .filter(([field, property]) => field !== 'symbol' && values[field] !== property.default)
      .map(([field]) => field);
  });
  /**
   * Changed parameters whose value would not reach the LEAN twin (#2904).
   *
   * The catalog names what a twin reads. A `lean_parameter_names` setting
   * travels as a strategy parameter, so only a blank one is lost: LEAN would
   * run its own default. A `lean_data_policy_parameter_names` setting travels
   * only as the strategy-bar cadence, so it is lost unless the policy this
   * run sends carries its value. Every other changed setting is lost.
   */
  readonly parametersLeanWouldIgnore = computed<readonly string[]>(() => {
    const strategy = this.selectedStrategy();
    const values = this.paramValues();
    const sent = new Set(strategy?.lean_parameter_names ?? []);
    const policyBacked = new Set(strategy?.lean_data_policy_parameter_names ?? []);
    const cadence = this.dataPolicy().strategy_bars.multiplier;
    return this.changedParameterNames().filter((name) => {
      if (sent.has(name)) return values[name] === undefined;
      if (policyBacked.has(name)) {
        return name !== strategy?.strategy_bars.parameter || values[name] !== cadence;
      }
      return true;
    });
  });
  /**
   * The strategy parameters a LEAN-only run sends: every setting the twin
   * reads, changed or not, as the parity companion sends them. `null` when
   * the twin takes none.
   */
  readonly leanStrategyParameters = computed<Record<string, unknown> | null>(() => {
    const names = this.selectedStrategy()?.lean_parameter_names ?? [];
    if (names.length === 0) return null;
    const values = this.paramValues();
    return Object.fromEntries(names.map((name) => [name, values[name]]));
  });

  readonly selectedStrategy = computed(() => {
    const name = this.selectedStrategyName();
    return name ? this.strategies().find((strategy) => strategy.name === name) ?? null : null;
  });
  /**
   * The lake tree this run will read. `both` sends `adjusted: false`, so its
   * picker must offer the raw tree — passed to the picker as an input rather
   * than pushed into the root-scoped catalog, which would outlive this page.
   */
  readonly adjustmentMode = computed<PriceAdjustmentMode>(() =>
    this.dataPolicy().adjusted ? "polygon_split_adjusted" : "raw",
  );

  /**
   * Whether this run reads lake bars at all. A direct LEAN run restored
   * from synthetic bars or a frozen fixture recording bypasses the lake
   * entirely (`lean_sidecar_service.py` generates or replays them), so what
   * the lake holds says nothing about whether it can rerun. Every other
   * engine choice goes through the Python engine (`runPython`), which
   * resolves the selected lake tree regardless of the policy's provenance —
   * `engine.py` only ever writes `provider_kind` itself, for a
   * compatibility snapshot taken from lake bytes — so those runs read the
   * lake whatever the restored policy says.
   */
  readonly readsLake = computed(() => {
    if (this.engine() !== "lean") return true;
    const policy = this.dataPolicy();
    return policy.source === "polygon" && policy.provider_kind === "live";
  });

  /**
   * The selected symbol is not in the tree this run would read.
   *
   * Switching the engine to `both` repoints the run at the raw tree, and the
   * symbol already chosen may exist only in the split-adjusted one. The picker
   * keeps displaying it — it is still the configured value — so without this
   * the run submits a symbol the tree cannot read and fails deep in the
   * engine. Only asserted once the catalog for that mode has actually
   * answered: a tree still loading, or a lake that is down, must not read as
   * "your symbol is wrong". A tree that answered *empty* does — the run would
   * read nothing — which is why this asks `resolved` rather than treating an
   * empty pool as "not yet known".
   *
   * Membership only matters when the run reads the tree as it stands. With
   * auto-fetch on (the default), the engine materializes the missing days
   * from the provider under the run's own mode before it reads
   * (`engine.py::_materialize_missing_bars`), so a symbol the raw tree lacks
   * is exactly what that option exists to fetch, not a reason to refuse.
   * A run that does not read the lake at all is never gated by it.
   */
  readonly symbolMissingFromTree = computed(() => {
    if (!this.readsLake() || this.autoFetch()) return false;
    const mode = this.adjustmentMode();
    // See instrument-card: `viewFor` may install a resource effect, which is
    // illegal inside a reactive context.
    const view = untracked(() => this.tickerCatalog.viewFor(mode));
    // `resolved` survives a reload (the dropdown re-reads on open), so the
    // retained verdict keeps blocking through that window instead of
    // briefly opening the run to a symbol already known to be absent.
    if (!view.resolved() || view.unavailable() !== null) return false;
    return !view.pool().some((option) => option.symbol === this.effectiveSymbol());
  });

  /**
   * The dates this run can read. A run that fetches missing days (auto-fetch)
   * is bounded by what the provider serves; a run that reads the tree as it
   * stands is bounded by the days the tree holds for the symbol; a run that
   * replays recorded bars reads no dates from the lake at all.
   */
  readonly dateBounds = computed<DateBounds>(() => {
    if (!this.readsLake()) return UNBOUNDED;
    if (this.autoFetch()) return this.providerBounds.bounds();
    const mode = this.adjustmentMode();
    const symbol = this.effectiveSymbol();
    return heldBoundsFor(untracked(() => this.tickerCatalog.viewFor(mode)), symbol);
  });

  /**
   * The sessions the tree lacks inside the held span, for a run that reads the
   * tree as it stands. A run that fetches missing days has no gaps to show.
   */
  readonly dateGaps = computed<DataGaps | null>(() => {
    if (!this.readsLake() || this.autoFetch()) return null;
    return this.heldGaps.gapsFor(this.adjustmentMode(), this.effectiveSymbol(), this.dateBounds(), this.resolution());
  });

  /** A date field holds no usable date: the picker hands over an empty one until it does. */
  readonly datesMissing = computed(() => this.range().from === "" || this.range().to === "");

  readonly rerunBlocked = computed(() => {
    const name = this.selectedStrategyName();
    return this.configurationWarning() !== null ||
      name === null ||
      this.symbolMissingFromTree() ||
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

    // Starts the window on the last month of what this run can read. A launch
    // link may name one end only: that end is kept and the other is filled, a
    // month before the given end or on the last readable day. A direct write,
    // not `changeRange`: nobody chose these dates, so they must not clear a
    // warning or release a restored selection.
    effect(() => {
      const range = this.range();
      if (range.from !== "" && range.to !== "") this.windowStarted = true;
      if (this.windowStarted) return;
      const bounds = this.dateBounds();
      // A given end past the last readable day cannot end the month: the picker refuses it.
      const unreadable = bounds.max !== null && range.to > bounds.max;
      const endingOn = range.to === "" || unreadable ? bounds : { ...bounds, max: range.to };
      const [lastMonth] = offerQuickRanges(["1M"], endingOn, this.calendar.sessions(), this.calendar.lastCompleted());
      if (lastMonth.range === null) return;
      const from = range.from === "" ? lastMonth.range.from : range.from;
      const to = range.to === "" ? lastMonth.range.to : range.to;
      untracked(() => this.range.update((current) => ({ ...current, from, to })));
    });

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
      this.strategiesRead.set(true);
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
    this.customLeanSource.set(null);
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
    if (engine !== "lean") this.customLeanSource.set(null);
    this.engine.set(engine);
    this.restoredDataPolicy.update((policy) => {
      if (policy === null) return null;
      const adjusted = engine !== "both";
      // A Both snapshot hashes one adjustment tree. Changing the tree starts
      // a new experiment; its old frozen receipt cannot describe those bars.
      if (policy.adjusted !== adjusted && policy.source === "polygon" &&
          policy.provider_kind === "fixture" && policy.fixture_id?.startsWith("bar-store-v1-")) {
        this.range.update((range) => ({ ...range, autoFetch: true }));
        return { ...policy, adjusted, provider_kind: "live", fixture_id: null, fixture_sha256: null };
      }
      return { ...policy, adjusted };
    });
    if (this.restoredDataPolicy() !== null) this.reconcileHistoricalSelection();
  }

  changeRange(range: TickerRange): void {
    const previous = this.range();
    // The picker hands over an empty date for a field it cannot use. That is
    // not a new configuration: it keeps the warning and the restored selection.
    if (onlyRefusesDates(previous, range)) {
      this.range.set(range);
      return;
    }
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
    // A link naming one date only has its other date filled from the one it names, not left as it was.
    const oneDate = (params.from === undefined) !== (params.to === undefined);
    if (oneDate) this.windowStarted = false;
    this.range.update((range) => ({
      ...range,
      symbol: params.symbol?.toUpperCase() ?? range.symbol,
      from: params.from ?? (oneDate ? "" : range.from),
      to: params.to ?? (oneDate ? "" : range.to),
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

function parseRunId(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
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

/** Whether ``next`` differs from ``previous`` only in dates that became empty. */
function onlyRefusesDates(previous: TickerRange, next: TickerRange): boolean {
  const refusedOrSame = (was: string, now: string): boolean => now === was || now === "";
  return (
    (next.from !== previous.from || next.to !== previous.to) &&
    refusedOrSame(previous.from, next.from) &&
    refusedOrSame(previous.to, next.to) &&
    next.symbol === previous.symbol &&
    next.resolution === previous.resolution &&
    next.multiplier === previous.multiplier &&
    next.session === previous.session &&
    next.autoFetch === previous.autoFetch
  );
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
