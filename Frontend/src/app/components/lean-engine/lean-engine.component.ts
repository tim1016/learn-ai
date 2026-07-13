import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  effect,
  inject,
  signal,
} from "@angular/core";
import { CommonModule } from "@angular/common";
import { FormsModule } from "@angular/forms";
import { ActivatedRoute, ParamMap, RouterModule } from "@angular/router";
import { HttpClient } from "@angular/common/http";
import { firstValueFrom, map } from "rxjs";
import { toSignal } from "@angular/core/rxjs-interop";
import { environment } from "../../../environments/environment";
import { ButtonModule } from "primeng/button";
import { Tab, TabList, TabPanel, TabPanels, Tabs } from "primeng/tabs";
import { RunReportComponent } from "../engine-lab/run-report/run-report.component";
import { EngineLabRunHistoryComponent } from "./engine-lab-run-history/engine-lab-run-history.component";
import { ValidationEvidenceCardComponent } from "./validation-evidence-card/validation-evidence-card.component";
import { ValidationStagePlaceholderComponent } from "./validation-stage-placeholder/validation-stage-placeholder.component";
import { StrategyDetailTabComponent } from "./strategy-detail-tab/strategy-detail-tab.component";
import { PageHeaderComponent } from "../../shared/page-header/page-header.component";
import {
  TickerRangePickerComponent,
  type AdvisoryAction,
  type AvailabilityCell,
  type AvailabilityStatus,
  type TickerRange,
} from "../../shared/ticker-range-picker";
import { TICKER_POOL, RECENT_TICKERS } from "../../shared/ticker-catalog";
import { JobsService } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import type { DataPolicy } from "../../models/data-policy";
import type {
  LeanLauncherDiagnosticReport,
  TrustedRunResponse,
} from "../../services/lean-sidecar.types";
import { RunDockComponent } from "../../shared/run-dock/run-dock.component";
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
} from "../../shared/run-dock/run-dock-source";
import { EngineRunDockSource } from "./engine-run-dock-source";
import { toMostRecentWeekday } from "../../shared/date/weekday";
import type { EngineValidationAnalytics } from "./engine-results/engine-validation-analytics.types";

/** Engine choice on the unified launch surface. */
export type EngineChoice = "python" | "lean" | "both";
type LeanLauncherStatus = "unknown" | "checking" | "ready" | "blocked";

/**
 * LEAN Engine — Phase 2 first cut.
 *
 * Talks directly to the PythonDataService at /api/engine/* (no GraphQL
 * wrapping yet — see docs/lean-engine-phase2-plan.md §2.4). Picks a
 * registered strategy, renders a parameter form dynamically from the
 * strategy's JSON Schema, runs a backtest, and displays summary stats and
 * the trade table. The Docs tab planned in §2.4a ships as a separate
 * component in a follow-up commit.
 */
export interface StrategyInfo {
  name: string;
  display_name: string;
  description: string;
  params_schema: ParamsSchema;
  /** Which on-disk LEAN resolutions this strategy can run against. */
  supported_resolutions: string[];
  /** Short pseudocode snippet of the entry/exit rules. May be empty. */
  algorithm_pseudocode?: string;
  /** Parity-critical gotchas surfaced from the validation studies.
   *  Render as a bullet list under the strategy description. */
  gotchas?: string[];
}

type EngineResolution = "minute" | "daily";

interface EngineLaunchParams {
  strategy?: string;
  engine?: EngineChoice;
  symbol?: string;
  from?: string;
  to?: string;
  resolution?: EngineResolution;
  tab?: "configuration" | "history" | "strategy";
}

function parseEngineLaunchParams(params: ParamMap): EngineLaunchParams {
  return {
    strategy: nonBlank(params.get("strategy")),
    engine: parseEngineChoice(params.get("engine")),
    symbol: nonBlank(params.get("symbol")),
    from: parseIsoDate(params.get("from")),
    to: parseIsoDate(params.get("to")),
    resolution: parseEngineResolution(params.get("resolution")),
    tab: parseLaunchTab(params.get("tab")),
  };
}

function parseEngineChoice(value: string | null): EngineChoice | undefined {
  return value === "python" || value === "lean" || value === "both" ? value : undefined;
}

function parseEngineResolution(value: string | null): EngineResolution | undefined {
  return isEngineResolution(value) ? value : undefined;
}

function parseLaunchTab(value: string | null): EngineLaunchParams["tab"] | undefined {
  if (value === "configuration" || value === "history" || value === "strategy") {
    return value;
  }
  return undefined;
}

function parseIsoDate(value: string | null): string | undefined {
  return value !== null && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : undefined;
}

function nonBlank(value: string | null): string | undefined {
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

function isEngineResolution(value: unknown): value is EngineResolution {
  return value === "minute" || value === "daily";
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

type RunPhase =
  | "idle"
  | "connecting"
  // ── Python engine phase ids (post-#471 unified taxonomy) ────────
  | "fetching_data"
  | "consolidating_bars"
  | "running_indicators"
  | "aggregating_results"
  | "persisting"
  // ── Legacy ids kept for transient cross-deploy compatibility.
  //    Will be removed once all environments have shipped the
  //    #471 backend changes.
  | "loading_bars"
  | "simulating"
  | "computing_stats"
  | "completed"
  | "failed";

interface ParamsSchema {
  title?: string;
  type?: string;
  properties?: Record<string, ParamProperty>;
  required?: string[];
}

interface ParamProperty {
  type?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  description?: string;
  title?: string;
}

interface EngineTrade {
  trade_number: number;
  entry_time: number;
  entry_price: number;
  exit_time: number;
  exit_price: number;
  quantity?: number;
  indicators: Record<string, number>;
  pnl_pts: number;
  pnl_pct: number;
  result: string;
  signal_reason: string;
}

interface EngineBacktestResponse {
  success: boolean;
  strategy_name: string;
  fill_mode: string;
  initial_cash: number;
  final_equity: number;
  net_profit: number;
  total_fees: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  statistics: Record<string, number | null>;
  lean_statistics: any | null;
  trades: EngineTrade[];
  log_lines: string[];
  equity_curve?: { timestamp: number; equity: number; cash: number; holdings_value: number }[];
  chart_bars?: { t: number; o: number; h: number; l: number; c: number; v: number }[];
  insights?: Record<string, any>[];
  insight_summary?: Record<string, any>;
  validation_analytics?: EngineValidationAnalytics | null;
  /** Auto-saved study row id, populated by the engine before returning so
   *  the Engine Lab can immediately enable the Replay tab. Null when the
   *  best-effort save call to the .NET backend failed. */
  study_id?: number | null;
  error?: string;
}

interface DataAvailability {
  symbol: string;
  start: string;
  end: string;
  resolution: string;
  expected_days: number;
  available_days: number;
  is_complete: boolean;
  missing_days: string[];
  sources: Record<string, string[]>;
}

@Component({
  selector: "app-lean-engine",
  imports: [
    CommonModule, FormsModule, RouterModule, ButtonModule,
    Tabs, TabList, Tab, TabPanel, TabPanels,
    RunReportComponent, EngineLabRunHistoryComponent,
    ValidationEvidenceCardComponent,
    ValidationStagePlaceholderComponent,
    StrategyDetailTabComponent,
    PageHeaderComponent,
    TickerRangePickerComponent,
    RunDockComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./lean-engine.component.html",
  styleUrls: ["./lean-engine.component.scss"],
  providers: [
    // Engine Lab's own dock source — maps JobsService state for engine-type
    // jobs onto the generic dock contract. Provided component-level so the
    // service lifecycle stays scoped to this page.
    EngineRunDockSource,
    { provide: RUN_DOCK_SOURCE, useExisting: EngineRunDockSource },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: "run-dock-expanded:engine-lab" },
  ],
})
export class LeanEngineComponent implements OnInit {
  private http = inject(HttpClient);
  private route = inject(ActivatedRoute);
  private jobsService = inject(JobsService);
  private leanSidecarService = inject(LeanSidecarService);
  private readonly apiBase = `${environment.pythonServiceUrl}/api/engine`;
  private readonly launchParams = toSignal(
    this.route.queryParamMap.pipe(map(parseEngineLaunchParams)),
    { initialValue: {} },
  );
  private readonly appliedLaunchParamsKey = signal<string | null>(null);

  /**
   * PR B.5 — engine choice for the unified launch surface. ``python``
   * runs the in-process engine via the jobs API; ``lean`` ships the
   * operator-typed QCAlgorithm source to the LEAN sidecar.
   */
  readonly engine = signal<EngineChoice>("python");

  /**
   * Operator-typed LEAN algorithm source. Two-way bound to
   * ``LeanScriptEditorComponent`` so the parent can synthesize the
   * trusted-run submission body without reaching into the child.
   */
  readonly leanLauncherCommand =
    "cd PythonDataService && PYTHONPATH=. ./.venv/bin/python -m uvicorn app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090";
  readonly leanLauncherStatus = signal<LeanLauncherStatus>("unknown");
  readonly leanLauncherDetail = signal<string>("");
  readonly leanLauncherCopied = signal(false);
  readonly leanLauncherBlocksRun = computed(
    () => this.engine() !== "python" && this.leanLauncherStatus() !== "ready",
  );

  // ------------------------------------------------------------------
  // State (signals)
  // ------------------------------------------------------------------
  readonly strategies = signal<StrategyInfo[]>([]);
  readonly strategiesLoading = signal(false);
  readonly strategiesError = signal<string | null>(null);

  readonly selectedStrategyName = signal<string | null>(null);
  /** Mutable parameter values keyed by field name — shaped by the picked schema. */
  readonly paramValues = signal<Record<string, unknown>>({});

  readonly resolution = signal<EngineResolution>("minute");
  readonly fillMode = signal<"signal_bar_close" | "next_bar_open">("signal_bar_close");
  // Default range = most recent one month ending yesterday. Matches the
  // Data Lab convention so users have a sensible window pre-populated
  // without any clicks. The quick-range buttons below can swap this out.
  readonly startDate = signal<string>(LeanEngineComponent.defaultStart());
  readonly endDate = signal<string>(LeanEngineComponent.defaultEnd());
  readonly initialCash = signal<number>(100000);

  readonly commissionPerOrder = signal<number>(1.0);

  /** Preset quick-range buttons — matches the Data Lab style so a user
   *  flipping between the two pages sees the same shortcuts. ``days`` is
   *  a count of calendar days back from yesterday; labels are kept short
   *  to fit a single inline row. */
  readonly rangePresets: readonly { label: string; days: number }[] = [
    { label: "1D", days: 1 },
    { label: "7D", days: 7 },
    { label: "15D", days: 15 },
    { label: "1M", days: 30 },
    { label: "3M", days: 90 },
    { label: "6M", days: 180 },
    { label: "12M", days: 365 },
    { label: "2Y", days: 730 },
  ];

  readonly activeTab = signal<string>("configuration");
  readonly strategyDetailName = signal<string | null>(null);
  readonly strategyDetail = computed(() => {
    const name = this.strategyDetailName();
    return name ? this.strategies().find((strategy) => strategy.name === name) ?? null : null;
  });

  readonly running = signal(false);

  // ------------------------------------------------------------------
  // Live status banner (SSE-driven) — surfaces the engine's current
  // phase + last log line while a run is in flight, then sticks around
  // briefly with a success/failure verdict so the user has a settled
  // confirmation before navigating to Results.
  // ------------------------------------------------------------------
  readonly runPhase = signal<RunPhase>('idle');
  readonly runStatusBanner = signal<string>('');
  readonly runPhaseDetail = signal<string>('');

  private setRunStatus(phase: RunPhase, headline: string, detail = ''): void {
    this.runPhase.set(phase);
    this.runStatusBanner.set(headline);
    this.runPhaseDetail.set(detail);
  }

  /** Persisted-run id rendered by the shared run report. The persisted
   *  run is the ONLY render source — there is no transient results path,
   *  so the workbench stage and /engine/runs/:id cannot diverge. */
  readonly completedRunId = signal<number | null>(null);
  readonly runError = signal<string | null>(null);

  // ------------------------------------------------------------------
  // Dynamic data layer state — availability + on-demand fetch
  // ------------------------------------------------------------------
  // Default ON: the typical Engine Lab user is iterating on a ticker that
  // is not guaranteed to live in the read-only reference mount, so having
  // auto-fetch disabled by default silently produced zero-trade runs. The
  // SPY bit-exact fixture still wins because the reference mount is
  // always checked first — auto-fetch only pulls what's actually missing.
  readonly autoFetch = signal<boolean>(true);
  readonly availability = signal<DataAvailability | null>(null);
  readonly availabilityLoading = signal<boolean>(false);
  readonly availabilityError = signal<string | null>(null);

  /** The symbol the engine will run against — read from the params form so
   *  we don't duplicate the strategy schema's own ``symbol`` field. SPY EMA
   *  crossover has no symbol field (it's hardcoded), so this falls back to
   *  SPY for reporting purposes. */
  readonly effectiveSymbol = computed<string>(() => {
    const fromParams = this.paramValues()["symbol"];
    if (typeof fromParams === "string" && fromParams.trim().length > 0) {
      return fromParams.trim().toUpperCase();
    }
    return "SPY";
  });

  /**
   * PR B (2026-05-19) — synthesize the canonical ``DataPolicy`` block
   * from the current form state. The shape mirrors the Python and .NET
   * sides exactly so the compare-view can gate on equality.
   *
   * The hidden defaults are documented in
   * `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md`
   * § 4.4: ``adjusted=true`` (pre-adjusted Polygon staging), regular
   * session, and input bars at the resolution toggle. ``strategy_bars``
   * carries the strategy's canonical evaluation cadence so LEAN and
   * Python persist comparable DataPolicy blocks.
   */
  composeDataPolicy(): DataPolicy {
    const timespan: DataPolicy['input_bars']['timespan'] =
      this.resolution() === 'daily' ? 'day' : 'minute';
    const strategyBars = this.strategyBarsSpec(timespan);
    return {
      source: 'polygon',
      symbol: this.effectiveSymbol(),
      adjusted: true,
      session: 'regular',
      input_bars: { timespan, multiplier: 1 },
      strategy_bars: strategyBars,
      timestamp_policy: 'bar_close_ms_utc',
      timezone: 'America/New_York',
      provider_kind: 'live',
      fixture_id: null,
      fixture_sha256: null,
    };
  }

  private strategyBarsSpec(
    timespan: DataPolicy['input_bars']['timespan'],
  ): DataPolicy['strategy_bars'] {
    const strategyName = this.selectedStrategyName();
    const isEmaCrossoverRun =
      strategyName === 'spy_ema_crossover' ||
      (strategyName === null && this.engine() === 'lean');
    if (timespan === 'minute' && isEmaCrossoverRun) {
      return { timespan, multiplier: 15 };
    }
    return { timespan, multiplier: 1 };
  }

  readonly tickerPool = TICKER_POOL;
  readonly recentTickers = RECENT_TICKERS;

  /** Writable source of truth for the shared picker. Kept as a
   *  ``signal`` (not ``computed``) so the picker's two-way binding
   *  isn't clobbered on every change-detection cycle by a re-evaluated
   *  parent expression. An ``effect`` below propagates writes into the
   *  legacy per-field signals that the availability check and the
   *  ``run()`` body read from. */
  readonly rangeState = signal<TickerRange>({
    symbol: "SPY",
    from: LeanEngineComponent.defaultStart(),
    to: LeanEngineComponent.defaultEnd(),
    resolution: "minute",
    autoFetch: true,
  });

  /** Per-day availability cells derived from the existing summary-level
   *  ``DataAvailability`` response. The backend only returns aggregate
   *  counts + a ``missing_days`` list today, so we iterate every weekday
   *  in the range and mark it complete / missing accordingly. Weekends
   *  render as faint placeholders. */
  readonly pickerAvailability = computed<readonly AvailabilityCell[]>(() => {
    const av = this.availability();
    const from = this.startDate();
    const to = this.endDate();
    if (!from || !to) return [];
    const missing = new Set(av?.missing_days ?? []);
    const cells: AvailabilityCell[] = [];
    const start = new Date(from + "T00:00:00Z");
    const end = new Date(to + "T00:00:00Z");
    for (let d = new Date(start); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
      const iso = d.toISOString().slice(0, 10);
      const dow = d.getUTCDay();
      let status: AvailabilityStatus;
      if (dow === 0 || dow === 6) status = "weekend";
      else if (missing.has(iso)) status = "missing";
      else if (av) status = "complete";
      else status = "missing";
      cells.push({ date: iso, status });
    }
    return cells;
  });

  onPickerAdvisoryAction(action: AdvisoryAction): void {
    if (action.triggerRun) {
      void this.run();
    }
    // ``refetchHoles`` is advisory for now — no backend endpoint yet.
  }

  /** Strategies filtered down to those that support the currently-selected
   *  engine resolution. The filter is applied at the registry level on the
   *  backend too, but mirroring it here keeps the dropdown honest: picking
   *  "Daily" should never let the user select a minute-only strategy and
   *  then eat a 400 at submit time. */
  readonly availableStrategies = computed<StrategyInfo[]>(() => {
    const res = this.resolution();
    return this.strategies().filter((s) =>
      (s.supported_resolutions ?? ["minute"]).includes(res)
    );
  });

  readonly selectedStrategy = computed(() => {
    const name = this.selectedStrategyName();
    if (!name) return null;
    return this.strategies().find((s) => s.name === name) ?? null;
  });

  readonly paramEntries = computed(() => {
    const schema = this.selectedStrategy()?.params_schema;
    if (!schema?.properties) return [];
    // ``symbol`` is driven by the shared ticker-range picker, not a free
    // form field — hide it from the generic params form so it isn't
    // shown twice.
    return Object.entries(schema.properties)
      .filter(([name]) => name !== "symbol")
      .map(([name, prop]) => ({ name, prop }));
  });

  readonly runBarContext = computed(() => {
    const strat = this.selectedStrategy();
    const symbol = this.effectiveSymbol();
    const from = this.startDate();
    const to = this.endDate();
    const res = this.resolution();
    if (!strat || !symbol || !from || !to) return null;
    return { name: strat.display_name, symbol, from, to, res };
  });

  constructor() {
    // Bridge JobsService events → run banner state. One effect per
    // engine; both react to the same SSE stream but read different
    // jobId signals so a Python run and a LEAN run can't tangle.
    this.wireEngineJobEffect();
    this.wireLeanJobEffect();

    // Keep the legacy per-field signals in sync with the picker's
    // writable ``rangeState``. These signals are what the availability
    // check and ``run()`` body read from, so this
    // effect is the single bridge between the new picker and the rest
    // of the component — no consumer below the picker had to change.
    effect(() => {
      const v = this.rangeState();
      this.startDate.set(v.from);
      this.endDate.set(v.to);
      this.resolution.set(v.resolution as EngineResolution);
      this.autoFetch.set(v.autoFetch ?? false);

      // Mirror the picker's symbol into the strategy's params dict if
      // the selected strategy exposes a ``symbol`` field. Otherwise the
      // picker's symbol is informational only (e.g. the SPY-hardcoded
      // EMA crossover).
      const schema = this.selectedStrategy()?.params_schema;
      if (schema?.properties && "symbol" in schema.properties) {
        const current = this.paramValues();
        if (current["symbol"] !== v.symbol) {
          this.paramValues.set({ ...current, symbol: v.symbol });
        }
      }
    });

    // Auto-refresh the availability report whenever the user changes
    // symbol, start date, or end date. We don't wait for a "Check" button
    // because that would make the UI feel sluggish and the endpoint is
    // a cheap on-disk scan — no Polygon call involved.
    effect(() => {
      const symbol = this.effectiveSymbol();
      const start = this.startDate();
      const end = this.endDate();
      const resolution = this.resolution();
      if (!symbol || !start || !end) {
        this.availability.set(null);
        return;
      }
      void this.checkAvailability(symbol, start, end, resolution);
    });

    // When the resolution changes, the current strategy selection may no
    // longer be valid (e.g. switching to daily while "SPY EMA crossover"
    // is selected). Rebind to the first strategy that supports the newly
    // chosen resolution so the form stays usable.
    effect(() => {
      const available = this.availableStrategies();
      const current = this.selectedStrategyName();
      if (available.length === 0) {
        return;
      }
      if (!current || !available.some((s) => s.name === current)) {
        this.onStrategyChange(available[0].name);
      }
    });

    // Deep links from Strategy Validation and run receipts land here.
    // Apply once after strategy metadata loads so URL state wins over
    // the first-compatible auto-selection above.
    effect(() => {
      const params = this.launchParams();
      const key = launchParamsKey(params);
      if (key === this.appliedLaunchParamsKey() || key === "||||||") {
        return;
      }
      if (this.strategies().length === 0) {
        return;
      }
      this.applyLaunchParams(params);
      this.appliedLaunchParamsKey.set(key);
    });
  }

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------
  ngOnInit(): void {
    this.loadStrategies();
  }

  async checkAvailability(
    symbol: string,
    start: string,
    end: string,
    resolution: EngineResolution
  ): Promise<void> {
    this.availabilityLoading.set(true);
    this.availabilityError.set(null);
    try {
      const url = `${this.apiBase}/data/availability`;
      const params = { symbol, start, end, resolution };
      const report = await firstValueFrom(
        this.http.get<DataAvailability>(url, { params })
      );
      this.availability.set(report);
    } catch (err: any) {
      this.availability.set(null);
      const detail = err?.error?.detail;
      this.availabilityError.set(
        typeof detail === "string"
          ? detail
          : err?.message ?? "Availability check failed"
      );
    } finally {
      this.availabilityLoading.set(false);
    }
  }

  async loadStrategies(): Promise<void> {
    this.strategiesLoading.set(true);
    this.strategiesError.set(null);
    try {
      const url = `${this.apiBase}/strategies`;
      const list = await firstValueFrom(this.http.get<StrategyInfo[]>(url));
      this.strategies.set(list);
      // Auto-select the first strategy compatible with the current
      // resolution so the form has something to render. The resolution
      // effect would pick this up too, but selecting here avoids a
      // one-frame flicker on initial load.
      const first = list.find((s) =>
        (s.supported_resolutions ?? ["minute"]).includes(this.resolution())
      );
      if (first) {
        this.onStrategyChange(first.name);
      }
    } catch (err) {
      this.strategiesError.set(
        err instanceof Error ? err.message : "Failed to load strategies"
      );
    } finally {
      this.strategiesLoading.set(false);
    }
  }

  // ------------------------------------------------------------------
  // Form handlers
  // ------------------------------------------------------------------
  onStrategyChange(name: string): void {
    this.selectedStrategyName.set(name);
    // Reset parameter values to the schema defaults.
    const schema = this.strategies().find((s) => s.name === name)
      ?.params_schema;
    const defaults: Record<string, unknown> = {};
    if (schema?.properties) {
      for (const [field, prop] of Object.entries(schema.properties)) {
        if (prop.default !== undefined) {
          defaults[field] = prop.default;
        }
      }
    }
    this.paramValues.set(defaults);
    this.completedRunId.set(null);
    this.runError.set(null);
  }

  private applyLaunchParams(params: EngineLaunchParams): void {
    if (params.engine) {
      this.engine.set(params.engine);
    }

    const strategy = params.strategy ? this.findLaunchStrategy(params.strategy) : null;
    const resolution = this.resolveLaunchResolution(strategy, params.resolution);
    this.applyLaunchRange(params, resolution);

    if (strategy) {
      this.onStrategyChange(strategy.name);
      this.applyUnsupportedResolutionWarning(strategy, params.resolution, resolution);
      if (params.symbol) {
        this.applyLaunchSymbolParam(params.symbol);
      }
      if (params.tab === "strategy") {
        this.strategyDetailName.set(strategy.name);
        this.activeTab.set(`strategy:${strategy.name}`);
      } else {
        this.activeTab.set(params.tab ?? "configuration");
      }
      return;
    }

    if (params.strategy) {
      this.runError.set(`Strategy "${params.strategy}" is not registered in Engine Lab.`);
    }
    this.activeTab.set(params.tab === "history" ? "history" : "configuration");
  }

  private findLaunchStrategy(key: string): StrategyInfo | null {
    const normalizedKey = LeanEngineComponent.normalizeStrategyKey(key);
    return this.strategies().find((strategy) =>
      strategy.name === key ||
      strategy.name.toLowerCase() === key.toLowerCase() ||
      LeanEngineComponent.normalizeStrategyKey(strategy.name) === normalizedKey
    ) ?? null;
  }

  private resolveLaunchResolution(
    strategy: StrategyInfo | null,
    requested: EngineResolution | undefined,
  ): EngineResolution {
    if (!strategy) {
      return requested ?? this.resolution();
    }

    const supported = (strategy.supported_resolutions ?? ["minute"]).filter(isEngineResolution);
    if (requested && supported.includes(requested)) {
      return requested;
    }
    return supported[0] ?? "minute";
  }

  private applyUnsupportedResolutionWarning(
    strategy: StrategyInfo,
    requested: EngineResolution | undefined,
    applied: EngineResolution,
  ): void {
    const supported = (strategy.supported_resolutions ?? ["minute"]).filter(isEngineResolution);
    if (requested && !supported.includes(requested)) {
      this.runError.set(
        `${strategy.display_name} does not support ${requested} data. Using ${applied} instead.`,
      );
    }
  }

  private applyLaunchRange(params: EngineLaunchParams, resolution: EngineResolution): void {
    const current = this.rangeState();
    const next: TickerRange = {
      ...current,
      symbol: params.symbol?.toUpperCase() ?? current.symbol,
      from: params.from ?? current.from,
      to: params.to ?? current.to,
      resolution,
    };
    this.rangeState.set(next);
    this.startDate.set(next.from);
    this.endDate.set(next.to);
    this.resolution.set(resolution);
    this.autoFetch.set(next.autoFetch ?? false);
  }

  private applyLaunchSymbolParam(symbol: string): void {
    const schema = this.selectedStrategy()?.params_schema;
    if (!schema?.properties || !("symbol" in schema.properties)) {
      return;
    }
    this.paramValues.set({ ...this.paramValues(), symbol: symbol.toUpperCase() });
  }

  private static normalizeStrategyKey(value: string): string {
    return value.toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  openStrategyDetail(name: string): void {
    this.onStrategyChange(name);
    this.strategyDetailName.set(name);
    this.activeTab.set(`strategy:${name}`);
  }

  closeStrategyDetail(): void {
    this.strategyDetailName.set(null);
    this.activeTab.set("configuration");
  }

  onParamChange(field: string, rawValue: string, type: string | undefined): void {
    const current = { ...this.paramValues() };
    if (type === "integer") {
      const n = parseInt(rawValue, 10);
      current[field] = Number.isNaN(n) ? undefined : n;
    } else if (type === "number") {
      const n = parseFloat(rawValue);
      current[field] = Number.isNaN(n) ? undefined : n;
    } else {
      current[field] = rawValue;
    }
    this.paramValues.set(current);
  }

  // ------------------------------------------------------------------
  // Run a backtest — driven by the global Jobs/SSE infrastructure.
  //
  // Flow:
  //   1. POST /api/jobs/engine_backtest with the EngineBacktestRequest
  //      payload nested under ``backtest`` (the .NET layer mints the
  //      job_id and forwards to /api/jobs-internal/engine-backtest).
  //   2. JobsService opens the SSE channel and updates a per-job
  //      signal as phase / log events arrive. A reactive effect bound
  //      to that signal drives the run banner — we don't talk to
  //      EventSource directly.
  //   3. On terminal job state (completed / failed / cancelled), fetch
  //      the JSON result blob and render Results / Replay as before.
  // ------------------------------------------------------------------
  private engineJobId = signal<string | null>(null);
  /** Active ``lean_engine_run`` job id (#470). Separate from
   *  ``engineJobId`` so the dock can show whichever engine is in
   *  flight without the two effects fighting over the run banner. */
  private leanJobId = signal<string | null>(null);

  async run(): Promise<void> {
    if (this.engine() === "both") {
      await Promise.all([this.runPython(), this.runLean()]);
      return;
    }
    if (this.engine() === "lean") {
      await this.runLean();
      return;
    }
    await this.runPython();
  }

  private async runPython(): Promise<void> {
    const name = this.selectedStrategyName();
    if (!name) return;

    this.running.set(true);
    this.runError.set(null);
    this.completedRunId.set(null);
    this.setRunStatus(
      "connecting",
      "Submitting backtest…",
      `${this.effectiveSymbol()} · ${this.startDate()} → ${this.endDate()}`,
    );

    // The backtest request is nested under a ``backtest`` key — the
    // .NET layer forwards the payload verbatim to the Python internal
    // endpoint, which validates the inner shape via
    // EngineBacktestRequest.
    const backtest: Record<string, unknown> = {
      strategy_name: name,
      fill_mode: this.fillMode(),
      initial_cash: this.initialCash(),
      commission_per_order: this.commissionPerOrder(),
      params: this.paramValues(),
      auto_fetch: this.autoFetch(),
      resolution: this.resolution(),
      // PR B (2026-05-19) — canonical DataPolicy block on every engine
      // submission. The Python router accepts the block as-is and echoes
      // it in the response; the persistence layer writes it into the new
      // ``DataPolicyJson`` column.
      data_policy: this.composeDataPolicy(),
    };
    if (this.startDate()) backtest["start_date"] = this.startDate();
    if (this.endDate()) backtest["end_date"] = this.endDate();

    try {
      const id = await this.jobsService.startJob("engine_backtest", { backtest });
      this.engineJobId.set(id);
      // The jobEffect (configured in the constructor) will flip
      // running()/setRunStatus()/result() as job events arrive.
    } catch (err: any) {
      const detail = err?.error?.detail;
      const message =
        typeof detail === "string" ? detail : err?.message ?? "Backtest request failed";
      this.runError.set(message);
      this.setRunStatus("failed", "Backtest request failed", message);
      this.updateRunningState();
    }
  }

  /**
   * PR B.5 — submit the operator-typed LEAN algorithm source to the
   * sidecar via the unified launch surface. The request body mirrors
   * ``TrustedRunRequest`` in ``lean-sidecar.types.ts``: a canonical
   * ``data_policy`` block plus ``algorithm_source`` and session-aligned
   * start/end millis.
   *
   * The component does not subscribe to LEAN-side progress events for
   * Phase 5 — the response shape carries the final status, so the
   * runner state is updated synchronously on resolution. A future
   * pass can stream sidecar logs through the run-banner the same way
   * the Python path does.
   */
  private async runLean(): Promise<void> {
    if (this.leanLauncherStatus() !== "ready") {
      await this.checkLeanLauncher();
      if (this.leanLauncherStatus() !== "ready") {
        const message = this.leanLauncherDetail() || "Start the LEAN launcher before running.";
        this.runError.set(message);
        this.setRunStatus("failed", "LEAN launcher unavailable", message);
        return;
      }
    }

    this.running.set(true);
    this.runError.set(null);
    this.completedRunId.set(null);
    this.setRunStatus(
      "connecting",
      "Submitting LEAN run…",
      `${this.effectiveSymbol()} · ${this.startDate()} → ${this.endDate()}`,
    );

    try {
      // Advance the operator-picked end date to the next NYSE
      // session's 09:30 ET open before submission. The sidecar's
      // window is half-open ``[start_ms_utc, end_ms_utc)`` per the
      // P2.5 contract; if we passed ``endDate()`` directly:
      //   * a same-day window collapses to start == end → 422, and
      //   * a multi-day window silently excludes ``endDate()`` itself
      //     because the orchestrator derives ``end_date`` from
      //     ``end_ms_utc - 1ms`` (the previous trading day).
      // Server-side resolution keeps the NYSE calendar in one place.
      const endResolution = await this.leanSidecarService.nextTradingDayOpen(
        this.endDate(),
      );
      // Route through the Jobs API instead of the blocking
      // ``startTrustedRun`` POST so the run dock surfaces phase
      // progress (staging_data → launching_sidecar → sidecar_running →
      // parsing_results → persisting) and the History tab auto-refresh
      // (#468) picks up the new ``StrategyExecution`` row the moment
      // ``job.completed`` fires. The underlying orchestrator
      // (``lean_sidecar_service.run_trusted_sample``) is unchanged —
      // see #470. The synchronous ``/api/lean-sidecar/trusted-runs``
      // endpoint stays callable for test infra and reconcile scripts.
      // The internal job route expects the trusted-run body under a
      // ``request`` sub-object (mirrors how ``engine_backtest`` wraps
      // its EngineBacktestRequest under ``backtest``) so the existing
      // ``TrustedRunRequestModel`` Pydantic schema stays the single
      // source of truth for shape validation. Without this wrapper the
      // python-side ``model_validate(req.request)`` would see ``{}``
      // and 400 with missing-fields.
      const id = await this.jobsService.startJob("lean_engine_run", {
        request: {
          run_id: this.composeRunId(),
          template: this.leanTemplateForSelectedStrategy(),
          starting_cash: this.initialCash(),
          start_ms_utc: this.composeStartMs(),
          end_ms_utc: endResolution.session_open_ms_utc,
          data_policy: this.composeDataPolicy(),
        },
      });
      this.leanJobId.set(id);
      // ``wireLeanJobEffect`` drives the rest of the UI from the SSE
      // stream — setRunStatus updates per phase, terminal events
      // mirror the runPython terminal handling.
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "LEAN run request failed";
      this.runError.set(message);
      this.setRunStatus("failed", "LEAN run request failed", message);
      this.updateRunningState();
    }
  }

  private leanTemplateForSelectedStrategy(): "ema_crossover" | "deployment_validation" {
    return this.selectedStrategyName() === "deployment_validation"
      ? "deployment_validation"
      : "ema_crossover";
  }

  /**
   * Build a sidecar-compatible run id. Slug must match
   * ``^[a-z0-9][a-z0-9_-]{2,63}$``. Use a UTC-millisecond suffix so
   * concurrent submissions don't collide.
   */
  composeRunId(): string {
    const symbol = this.effectiveSymbol().toLowerCase().replace(/[^a-z0-9]/g, "");
    const stamp = Date.now().toString(36);
    return `engine_lab_${symbol}_${stamp}`;
  }

  async checkLeanLauncher(): Promise<void> {
    this.leanLauncherStatus.set("checking");
    this.leanLauncherDetail.set("");
    try {
      const report = await this.leanSidecarService.diagnose();
      this.applyLeanLauncherReport(report);
    } catch (err) {
      this.leanLauncherStatus.set("blocked");
      this.leanLauncherDetail.set(err instanceof Error ? err.message : "Launcher check failed.");
    }
  }

  async copyLeanLauncherCommand(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.leanLauncherCommand);
      this.leanLauncherCopied.set(true);
    } catch {
      this.leanLauncherCopied.set(false);
    }
  }

  private applyLeanLauncherReport(report: LeanLauncherDiagnosticReport): void {
    const launcher = report.checks.find((check) => check.name === "launcher_healthz");
    if (launcher?.status === "pass") {
      this.leanLauncherStatus.set("ready");
      this.leanLauncherDetail.set(launcher.detail);
      return;
    }
    this.leanLauncherStatus.set("blocked");
    this.leanLauncherDetail.set(launcher?.fix ?? launcher?.detail ?? "LEAN launcher is not reachable.");
  }

  /**
   * 09:30 ET on ``startDate()``, expressed as int64 ms UTC. Mirrors
   * the Python ``trading_calendar.session_open_ms_utc`` policy from
   * PR A so the sidecar's window-validation accepts the request.
   * Inline DST-aware: forms a wall-clock string, then parses it as
   * an ET instant.
   */
  composeStartMs(): number {
    return this.sessionOpenMs(this.startDate());
  }

  /**
   * 09:30 ET on ``endDate()``, expressed as int64 ms UTC.
   *
   * NOTE: The LEAN submission path does NOT use this value directly —
   * ``runLean()`` calls ``leanSidecarService.nextTradingDayOpen()`` to
   * advance to the next NYSE session's 09:30 ET so the half-open
   * ``[start_ms_utc, end_ms_utc)`` contract includes the operator's
   * chosen end date. This helper remains for tests / non-LEAN callers
   * that want the raw 09:30 ET ms of ``endDate()`` itself.
   */
  composeEndMs(): number {
    return this.sessionOpenMs(this.endDate());
  }

  /**
   * Convert ``YYYY-MM-DD`` + ``09:30`` ET to int64 ms UTC. Uses
   * ``Intl.DateTimeFormat`` to look up the America/New_York offset at
   * the target instant (DST-aware: EST is UTC-5, EDT is UTC-4).
   *
   * Algorithm: treat the wall-clock as if it were UTC to get a probe
   * instant, ask Intl for what ET would call that probe, compute the
   * offset, then subtract.
   */
  private sessionOpenMs(isoDate: string): number {
    if (!isoDate) return 0;
    const [y, m, d] = isoDate.split("-").map(Number);
    const probe = Date.UTC(y, m - 1, d, 9, 30, 0);
    const offsetMs = this.etOffsetMs(new Date(probe));
    return probe - offsetMs;
  }

  /**
   * ET offset (UTC - ET) for the supplied instant, in milliseconds.
   * Always negative for America/New_York (ET is behind UTC).
   * DST-aware via ``Intl.DateTimeFormat``.
   */
  private etOffsetMs(instant: Date): number {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
      .formatToParts(instant)
      .reduce<Record<string, string>>((acc, p) => {
        if (p.type !== "literal") acc[p.type] = p.value;
        return acc;
      }, {});
    const hh = parts["hour"] === "24" ? "00" : parts["hour"];
    const etAsUtc = Date.UTC(
      Number(parts["year"]),
      Number(parts["month"]) - 1,
      Number(parts["day"]),
      Number(hh),
      Number(parts["minute"]),
      Number(parts["second"]),
    );
    return etAsUtc - instant.getTime();
  }

  /** Reactive bridge from JobsService → banner state.
   *
   *  Subscribed once in the constructor. Reads the JobState for the
   *  active engine job id (if any) and translates phase / status into
   *  the banner's signals. On terminal status, fetches the result
   *  blob, then unlocks Replay / jumps to the Results tab. */
  private wireEngineJobEffect(): void {
    effect(() => {
      const id = this.engineJobId();
      if (!id) return;
      const job = this.jobsService.job(id);
      if (!job) return;

      const lastLog = job.recentLogs[job.recentLogs.length - 1]?.message ?? "";

      if (job.status === "queued" || job.status === "running") {
        const phase = (job.phase ?? "connecting") as RunPhase;
        // Headline mappings for the unified phase taxonomy (#471). The
        // legacy ids stay here for the deploy window where the .NET
        // layer is forwarding events from a python-service that hasn't
        // shipped the new emissions yet.
        const headlines: Record<string, string> = {
          connecting: "Submitting backtest…",
          // ── #471 taxonomy ────────────────────────────────────────
          fetching_data: "Fetching bars from data provider…",
          consolidating_bars: "Consolidating bars to strategy resolution…",
          running_indicators: "Running indicators and strategy logic…",
          aggregating_results: "Aggregating results and statistics…",
          persisting: "Persisting run to history…",
          // ── pre-#471 legacy ids (transient) ──────────────────────
          loading_bars: "Loading bars from cache & Polygon…",
          simulating: "Running engine — consolidating bars and evaluating signals…",
          computing_stats: "Computing statistics & saving study…",
        };
        this.setRunStatus(phase, headlines[phase] ?? `Phase: ${phase}`, lastLog);
        return;
      }

      if (job.status === "failed") {
        const message = job.errorMessage ?? "Backtest failed";
        this.runError.set(message);
        this.setRunStatus("failed", "Backtest failed", message);
        this.engineJobId.set(null);
        this.updateRunningState();
        return;
      }

      if (job.status === "cancelled") {
        this.setRunStatus("failed", "Backtest cancelled", job.message ?? "");
        this.engineJobId.set(null);
        this.updateRunningState();
        return;
      }

      if (job.status === "completed") {
        // Fetch the JSON result blob, then drive the rest of the UI.
        // We clear engineJobId synchronously so re-firings of this
        // effect during the async fetch don't double-handle.
        this.engineJobId.set(null);
        void this.handleEngineJobCompleted(id);
      }
    });
  }

  /**
   * Mirror of ``wireEngineJobEffect`` for the LEAN sidecar's
   * ``lean_engine_run`` job (#470). The terminal handling diverges:
   * LEAN returns a ``TrustedRunResult`` shape that the Results tab's
   * ``EngineBacktestResponse`` renderer can't consume directly. v1
   * keeps the user on the Configure tab with a "completed" headline
   * — the new ``StrategyExecution`` row appears on the History tab
   * via #468's auto-refresh, which is where LEAN runs were always
   * inspected anyway. A LEAN-specific Results renderer is a follow-up.
   */
  private wireLeanJobEffect(): void {
    effect(() => {
      const id = this.leanJobId();
      if (!id) return;
      const job = this.jobsService.job(id);
      if (!job) return;

      const lastLog = job.recentLogs[job.recentLogs.length - 1]?.message ?? "";

      if (job.status === "queued" || job.status === "running") {
        const phase = job.phase ?? "connecting";
        const leanHeadlines: Record<string, string> = {
          connecting: "Submitting LEAN run…",
          staging_data: "Staging LEAN data fixtures…",
          launching_sidecar: "Submitting launch request to the LEAN sidecar…",
          sidecar_running: "LEAN container running…",
          parsing_results: "Parsing LEAN output…",
          persisting: "Persisting run to history…",
        };
        const headline = leanHeadlines[phase] ?? `Phase: ${phase}`;
        // The local ``RunPhase`` union doesn't enumerate every LEAN
        // phase id, but ``setRunStatus`` only uses it as a CSS hook —
        // cast through the same coarse states the Python engine uses.
        const coarsePhase: RunPhase =
          phase === "connecting"
            ? "connecting"
            : phase === "persisting"
              ? "computing_stats"
              : phase === "parsing_results"
                ? "computing_stats"
                : "simulating";
        this.setRunStatus(coarsePhase, headline, lastLog);
        return;
      }

      if (job.status === "failed") {
        const message = job.errorMessage ?? "LEAN run failed";
        this.runError.set(message);
        this.setRunStatus("failed", "LEAN run failed", message);
        this.leanJobId.set(null);
        this.updateRunningState();
        return;
      }

      if (job.status === "cancelled") {
        this.setRunStatus("failed", "LEAN run cancelled", job.message ?? "");
        this.leanJobId.set(null);
        this.updateRunningState();
        return;
      }

      if (job.status === "completed") {
        this.leanJobId.set(null);
        void this.handleLeanJobCompleted(id);
      }
    });
  }

  private async handleEngineJobCompleted(jobId: string): Promise<void> {
    try {
      const response = await this.jobsService.fetchResult<EngineBacktestResponse>(jobId);
      if (response.error) {
        this.runError.set(response.error);
        this.setRunStatus("failed", "Backtest failed", response.error);
      } else if (response.success) {
        this.setRunStatus(
          "completed",
          `Completed — ${response.total_trades} trade${response.total_trades === 1 ? "" : "s"}, net ${this.formatCurrency(response.net_profit)}`,
        );
        if (response.study_id != null) {
          // The persisted run is the only render source — the report
          // reads back exactly what history will show for this run.
          this.completedRunId.set(response.study_id);
        } else {
          this.runError.set(
            "Run completed but persistence failed — no report available. The run was not saved to history; check backend logs.",
          );
        }
        this.activeTab.set("configuration");
      }
    } catch (err: any) {
      const message = err?.message ?? "Failed to fetch backtest result";
      this.runError.set(message);
      this.setRunStatus("failed", "Failed to fetch backtest result", message);
    } finally {
      this.updateRunningState();
    }
  }

  private async handleLeanJobCompleted(jobId: string): Promise<void> {
    try {
      const response = await this.jobsService.fetchResult<TrustedRunResponse>(jobId);
      if (response.strategy_execution_id !== null) {
        // LEAN runs render the same persisted report as Python runs.
        this.completedRunId.set(response.strategy_execution_id);
        this.setRunStatus(
          "completed",
          "LEAN run finished",
          `Persisted as study #${response.strategy_execution_id}.`,
        );
      } else {
        this.runError.set(
          "LEAN run completed but persistence failed — no report available. The run was not saved to history; check backend logs.",
        );
        this.setRunStatus("completed", "LEAN run finished", "Run completed, but no persisted study ID was returned.");
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to fetch LEAN run result";
      this.runError.set(message);
      this.setRunStatus("failed", "LEAN result unavailable", message);
    } finally {
      this.updateRunningState();
    }
  }

  private updateRunningState(): void {
    this.running.set(this.engineJobId() !== null || this.leanJobId() !== null);
  }

  // ------------------------------------------------------------------
  // Display helpers
  // ------------------------------------------------------------------
  formatCurrency(value: number | null | undefined): string {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    }).format(value);
  }

  // ------------------------------------------------------------------
  // Quick date-range presets — mirrors the Data Lab shortcut row so a
  // user flipping between the two pages stays oriented. Each button
  // snaps the end date to *yesterday* (latest full trading day from
  // Polygon's perspective) and walks the start back by N calendar days.
  // ------------------------------------------------------------------
  setPresetRange(daysBack: number): void {
    const end = LeanEngineComponent.yesterday();
    const start = new Date(end);
    start.setDate(start.getDate() - daysBack);
    this.startDate.set(LeanEngineComponent.toIso(toMostRecentWeekday(start)));
    this.endDate.set(LeanEngineComponent.toIso(toMostRecentWeekday(end)));
  }

  private static yesterday(): Date {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    d.setHours(0, 0, 0, 0);
    return d;
  }

  private static toIso(d: Date): string {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  // Walk back to the most recent weekday. The lean sidecar rejects any
  // start that isn't a session open (lean_sidecar.py:374) and we don't
  private static defaultEnd(): string {
    return LeanEngineComponent.toIso(toMostRecentWeekday(LeanEngineComponent.yesterday()));
  }

  private static defaultStart(): string {
    const end = LeanEngineComponent.yesterday();
    const start = new Date(end);
    start.setDate(start.getDate() - 30);
    return LeanEngineComponent.toIso(toMostRecentWeekday(start));
  }

  // ------------------------------------------------------------------
  // History → render the persisted run through the shared report
  // ------------------------------------------------------------------
  onStudySelected(studyId: number): void {
    this.completedRunId.set(studyId);
    this.activeTab.set("configuration");
  }

  availabilitySourceChips(av: DataAvailability): { label: string; days: number }[] {
    return Object.entries(av.sources || {}).map(([label, days]) => ({
      label,
      days: Array.isArray(days) ? days.length : 0,
    }));
  }
}
