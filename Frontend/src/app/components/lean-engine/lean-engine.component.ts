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
import { RouterModule } from "@angular/router";
import { HttpClient } from "@angular/common/http";
import { firstValueFrom } from "rxjs";
import { environment } from "../../../environments/environment";
import { ButtonModule } from "primeng/button";
import { Tab, TabList, TabPanel, TabPanels, Tabs } from "primeng/tabs";
import { EngineResultsComponent } from "./engine-results/engine-results.component";
import { StudyListItem } from "./study-list-item";
import { EngineLabRunHistoryComponent } from "./engine-lab-run-history/engine-lab-run-history.component";
import { LeanEngineDocsComponent } from "./lean-engine-docs/lean-engine-docs.component";
import { ChartBar, EngineTradeForChart, EquityCurvePoint } from "./engine-chart/engine-chart.component";
import { InsightPanelComponent } from "./insight-panel/insight-panel.component";
import { TvCompatPanelComponent } from "./tv-compat-panel/tv-compat-panel.component";
import { EngineReplayV2Component } from "./engine-replay-v2/engine-replay-v2.component";
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
import { RunDockComponent } from "../../shared/run-dock/run-dock.component";
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
} from "../../shared/run-dock/run-dock-source";
import { EngineRunDockSource } from "./engine-run-dock-source";
import { LeanScriptEditorComponent } from "../lean-script-editor/lean-script-editor.component";
import { EMA_CROSSOVER_SOURCE_TEMPLATE } from "../lean-script-editor/lean-script-editor.template";
import { toMostRecentWeekday } from "../../shared/date/weekday";

/** Engine choice on the unified launch surface. */
export type EngineChoice = "python" | "lean";

// Severity for pre-flight: re-declared locally so we don't import the panel's types.
type PreflightSeverity = "ok" | "warning" | "blocking";
interface PreflightSnapshot {
  overall: PreflightSeverity;
  summary: string;
}

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
interface StrategyInfo {
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
  /** True when the backend can generate a Pine v6 script for this
   *  strategy — controls visibility of the download button. */
  pine_available?: boolean;
}

type EngineResolution = "minute" | "daily";

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
  entry_time: string;
  entry_price: number;
  exit_time: string;
  exit_price: number;
  indicators: Record<string, number>;
  pnl_pts: number;
  pnl_pct: number;
  result: string;
  signal_reason: string;
}

/**
 * StudyTradeItem (camelCase) shape from GET /api/studies/{id}.
 * Defined narrow + exported so the trade-mapper helper has a typed input
 * and can be unit-tested in isolation.
 */
export interface StudyTradeApiItem {
  entryTimestamp: string;
  exitTimestamp: string;
  entryPrice: number;
  exitPrice: number;
  pnL: number;
  signalReason?: string | null;
}

/**
 * Reconstruct an EngineTrade from a persisted StudyTradeItem. The .NET
 * BacktestTrade entity does not store the percent return, so derive it
 * from `pnL / entryPrice` — matches the Python engine's definition
 * (`pnl_pct = pnl_pts / entry.entry_price`). Guard against entry_price
 * <= 0 to avoid NaN/Infinity propagating into the trade-log table.
 */
export function mapStudyTradeToEngineTrade(
  t: StudyTradeApiItem,
  index: number,
): EngineTrade {
  const pnlPct = t.entryPrice > 0 ? t.pnL / t.entryPrice : 0;
  return {
    trade_number: index + 1,
    entry_time: t.entryTimestamp,
    entry_price: t.entryPrice,
    exit_time: t.exitTimestamp,
    exit_price: t.exitPrice,
    pnl_pts: t.pnL,
    pnl_pct: pnlPct,
    result: t.pnL > 0 ? 'WIN' : 'LOSS',
    signal_reason: t.signalReason ?? '',
    indicators: {},
  };
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
  equity_curve?: { timestamp: string; equity: number; cash: number; holdings_value: number }[];
  chart_bars?: { t: number; o: number; h: number; l: number; c: number; v: number }[];
  insights?: Record<string, any>[];
  insight_summary?: Record<string, any>;
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
    EngineResultsComponent, LeanEngineDocsComponent, EngineLabRunHistoryComponent,
    InsightPanelComponent,
    TvCompatPanelComponent, EngineReplayV2Component,
    PageHeaderComponent,
    TickerRangePickerComponent,
    LeanScriptEditorComponent,
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
  private jobsService = inject(JobsService);
  private leanSidecarService = inject(LeanSidecarService);
  private readonly apiBase = `${environment.pythonServiceUrl}/api/engine`;

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
  readonly leanSource = signal<string>(EMA_CROSSOVER_SOURCE_TEMPLATE);

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

  // Timezone used for rendering entry_time / exit_time in the trades table
  // AND for the CSV download — whatever the table shows is what the file gets.
  readonly selectedTimezone = signal<string>("UTC");
  readonly commissionPerOrder = signal<number>(1.0);

  /** Curated list of commonly useful zones, plus the browser's detected local
   *  zone (only appended if it isn't already in the curated list). */
  readonly timezoneOptions = computed<{ value: string; label: string }[]>(() => {
    const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const base = [
      { value: "UTC", label: "UTC" },
      { value: "America/New_York", label: "New York (ET)" },
      { value: "America/Los_Angeles", label: "Los Angeles (PT)" },
      { value: "Europe/London", label: "London" },
      { value: "Asia/Kolkata", label: "Mumbai (IST)" },
      { value: "Asia/Tokyo", label: "Tokyo (JST)" },
    ];
    return base.some((o) => o.value === localZone)
      ? base
      : [...base, { value: localZone, label: `Local (${localZone})` }];
  });

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

  readonly activeTab = signal<string>("0");

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

  // ------------------------------------------------------------------
  // Replay — always enabled; the tab unlocks once a study (just-run or
  // selected from History) is available.
  // ------------------------------------------------------------------
  readonly replayEnabled = true;
  readonly selectedStudyForReplay = signal<StudyListItem | null>(null);

  // ------------------------------------------------------------------
  // TV-compatibility pre-flight
  // ------------------------------------------------------------------
  /** Latest snapshot from the embedded TvCompatPanel. Null until first emission. */
  readonly preflight = signal<PreflightSnapshot | null>(null);
  /** Run button is disabled while pre-flight reports `blocking`. */
  readonly preflightBlocks = computed(() => this.preflight()?.overall === "blocking");
  /** Indicators the current strategy uses — fed to the pre-flight panel. */
  readonly strategyIndicators = computed<readonly { name: string; length: number }[]>(() => {
    const name = this.selectedStrategyName();
    // Hard-coded canonical mapping for the three reference strategies. Once
    // strategies expose a metadata endpoint, read it from there instead.
    if (name === "SpyEmaCrossover" || name === "spy_ema_crossover") {
      return [{ name: "ema", length: 5 }, { name: "ema", length: 10 }, { name: "rsi", length: 14 }];
    }
    if (name === "RsiMeanReversion" || name === "rsi_mean_reversion") {
      return [{ name: "rsi", length: 14 }];
    }
    if (name === "SmaCrossover" || name === "sma_crossover") {
      return [{ name: "sma", length: 50 }, { name: "sma", length: 200 }];
    }
    // Fallback — unknown strategy, provide a safe conservative indicator so
    // pre-flight still runs and surfaces session/warmup feedback.
    return [{ name: "ema", length: 200 }];
  });
  /** Timeframe value passed to the pre-flight — derived from resolution. */
  readonly preflightTimeframe = computed<"5m" | "15m" | "1h">(() => {
    // The lean-engine currently supports minute + daily. Map to the pre-flight
    // timeframes; when lean-engine adds 5m/1h explicitly, update this.
    const res = this.resolution();
    return res === "daily" ? "1h" : "15m";
  });

  onPreflightUpdate(snapshot: PreflightSnapshot | null): void {
    this.preflight.set(snapshot);
  }
  readonly result = signal<EngineBacktestResponse | null>(null);
  readonly runError = signal<string | null>(null);

  // Computed chart data derived from backtest result
  readonly chartBars = computed<ChartBar[]>(() => {
    return this.result()?.chart_bars ?? [];
  });
  readonly chartTrades = computed<EngineTradeForChart[]>(() => {
    const r = this.result();
    if (!r?.trades) return [];
    return r.trades.map(t => ({
      entry_time: t.entry_time,
      exit_time: t.exit_time,
      entry_price: t.entry_price,
      exit_price: t.exit_price,
      pnl_pts: t.pnl_pts,
      result: t.result,
    }));
  });
  readonly equityCurve = computed<EquityCurvePoint[]>(() => {
    return (this.result()?.equity_curve ?? []).map(pt => ({
      timestamp: pt.timestamp,
      equity: pt.equity,
    }));
  });
  readonly insights = computed(() => this.result()?.insights ?? []);
  readonly insightSummary = computed(() => this.result()?.insight_summary ?? {});

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
   * session, ``input_bars`` and ``strategy_bars`` both at the
   * timeframe carried by the resolution toggle. Intra-strategy
   * consolidation (e.g., minute-1 → minute-15) lives inside the
   * strategy code itself, not in DataPolicy.
   */
  composeDataPolicy(): DataPolicy {
    const timespan: DataPolicy['input_bars']['timespan'] =
      this.resolution() === 'daily' ? 'day' : 'minute';
    return {
      source: 'polygon',
      symbol: this.effectiveSymbol(),
      adjusted: true,
      session: 'regular',
      input_bars: { timespan, multiplier: 1 },
      strategy_bars: { timespan, multiplier: 1 },
      timestamp_policy: 'bar_close_ms_utc',
      timezone: 'America/New_York',
      provider_kind: 'live',
      fixture_id: null,
      fixture_sha256: null,
    };
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
      this.activeTab.set("1");
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
    // Bridge JobsService events → run banner state.
    this.wireEngineJobEffect();

    // Keep the legacy per-field signals in sync with the picker's
    // writable ``rangeState``. These signals are what the availability
    // check, preflight panel, and ``run()`` body read from, so this
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
    }, { allowSignalWrites: true });

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
    this.result.set(null);
    this.runError.set(null);
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

  async run(): Promise<void> {
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
    this.result.set(null);
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
      this.running.set(false);
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
    this.running.set(true);
    this.runError.set(null);
    this.result.set(null);
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
      const response = await this.leanSidecarService.startTrustedRun({
        run_id: this.composeRunId(),
        algorithm_source: this.leanSource(),
        starting_cash: this.initialCash(),
        start_ms_utc: this.composeStartMs(),
        end_ms_utc: endResolution.session_open_ms_utc,
        data_policy: this.composeDataPolicy(),
      });
      // A "clean" run with the benign benchmark-unavailable bucket
      // populated produced trades and STATISTICS:: but LEAN's
      // post-strategy housekeeping (default SPY benchmark) failed.
      // Surface that to the operator inline so alpha/beta zeros in the
      // stats panel aren't read as a strategy result.
      const benchmarkMissing =
        (response.lean_errors?.benchmark_unavailable?.length ?? 0) > 0;
      this.setRunStatus(
        response.is_clean ? "completed" : "failed",
        response.is_clean
          ? benchmarkMissing
            ? `LEAN run finished cleanly (run id ${response.run_id}). Note: SPY benchmark was unavailable, so alpha/beta in stats are zero.`
            : `LEAN run finished cleanly (run id ${response.run_id}).`
          : `LEAN run finished with errors (exit ${response.exit_code}).`,
      );
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "LEAN run request failed";
      this.runError.set(message);
      this.setRunStatus("failed", "LEAN run request failed", message);
    } finally {
      this.running.set(false);
    }
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
        this.running.set(false);
        this.engineJobId.set(null);
        this.jobsService.dismiss(id);
        return;
      }

      if (job.status === "cancelled") {
        this.setRunStatus("failed", "Backtest cancelled", job.message ?? "");
        this.running.set(false);
        this.engineJobId.set(null);
        this.jobsService.dismiss(id);
        return;
      }

      if (job.status === "completed") {
        // Fetch the JSON result blob, then drive the rest of the UI.
        // We clear engineJobId synchronously so re-firings of this
        // effect during the async fetch don't double-handle.
        this.engineJobId.set(null);
        void this.handleEngineJobCompleted(id);
      }
    }, { allowSignalWrites: true });
  }

  private async handleEngineJobCompleted(jobId: string): Promise<void> {
    try {
      const response = await this.jobsService.fetchResult<EngineBacktestResponse>(jobId);
      this.result.set(response);
      if (response.error) {
        this.runError.set(response.error);
        this.setRunStatus("failed", "Backtest failed", response.error);
      } else if (response.success) {
        this.setRunStatus(
          "completed",
          `Completed — ${response.total_trades} trade${response.total_trades === 1 ? "" : "s"}, net ${this.formatCurrency(response.net_profit)}`,
        );
        if (response.study_id != null) {
          this.selectedStudyForReplay.set(this.synthesizeStudyForReplay(response));
        }
        this.activeTab.set("1");
      }
    } catch (err: any) {
      const message = err?.message ?? "Failed to fetch backtest result";
      this.runError.set(message);
      this.setRunStatus("failed", "Failed to fetch backtest result", message);
    } finally {
      this.running.set(false);
      this.jobsService.dismiss(jobId);
    }
  }

  /** Build a StudyListItem-shaped object from the just-finished backtest
   *  response so the Replay component can drive itself off it without
   *  refetching from /api/studies. Only the fields the replay actually
   *  reads (id, symbol, strategyName, startDate, endDate, timespan)
   *  carry meaningful values; everything else is filled with safe
   *  defaults so the type lines up. */
  private synthesizeStudyForReplay(r: EngineBacktestResponse): StudyListItem {
    return {
      id: r.study_id as number,
      symbol: this.effectiveSymbol(),
      strategyName: r.strategy_name,
      startDate: this.startDate(),
      endDate: this.endDate(),
      timespan: this.resolution(),
      fillMode: r.fill_mode,
      source: "engine",
      totalTrades: r.total_trades,
      winningTrades: r.winning_trades,
      losingTrades: r.losing_trades,
      winRate: r.win_rate,
      totalPnL: r.net_profit,
      maxDrawdown: (r.statistics?.["max_drawdown_pct"] as number) ?? 0,
      sharpeRatio: (r.statistics?.["sharpe_ratio"] as number) ?? 0,
      sortinoRatio: (r.statistics?.["sortino_ratio"] as number) ?? 0,
      compoundingAnnualReturn: 0,
      probabilisticSharpeRatio: 0,
      profitFactor: (r.statistics?.["profit_factor"] as number) ?? 0,
      valueAtRisk95: 0,
      alpha: 0,
      beta: 0,
      initialCash: r.initial_cash,
      finalEquity: r.final_equity,
      parameters: JSON.stringify(this.paramValues()),
      notes: null,
      executedAt: new Date().toISOString(),
      durationMs: 0,
    };
  }

  readonly pineDownloading = signal(false);
  readonly pineError = signal<string | null>(null);

  /** Fetch a Pine v6 script for the current strategy + params and
   *  trigger a browser download. Disabled when the selected strategy
   *  does not expose a generator (``pine_available === false``). */
  async downloadPineScript(): Promise<void> {
    const strategy = this.selectedStrategy();
    if (!strategy?.pine_available) {
      this.pineError.set("No Pine script is available for this strategy.");
      return;
    }
    this.pineDownloading.set(true);
    this.pineError.set(null);
    try {
      const url = `${this.apiBase}/strategies/${strategy.name}/pine`;
      const text = await firstValueFrom(
        this.http.post(url, this.paramValues(), { responseType: "text" })
      );
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = `${strategy.name}.pine`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(objectUrl);
    } catch (err: any) {
      const detail = err?.error?.detail;
      this.pineError.set(
        typeof detail === "string"
          ? detail
          : err?.message ?? "Pine download failed"
      );
    } finally {
      this.pineDownloading.set(false);
    }
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

  formatPercent(value: number | null | undefined): string {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return `${(value * 100).toFixed(2)}%`;
  }

  formatNumber(value: number | null | undefined, places = 2): string {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return value.toFixed(places);
  }

  /**
   * Format a backend ISO-8601 timestamp in the currently selected timezone,
   * emitting ISO-8601 with a numeric offset (e.g. 2025-04-17T10:00:00-04:00).
   * UTC is rendered with a trailing 'Z'. Returned string is what the table
   * cell displays AND what the CSV row writes for that field, so the two
   * stay in lockstep regardless of which zone is picked.
   */
  formatTradeTime(iso: string): string {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso; // fail-soft: keep raw

    const zone = this.selectedTimezone();
    if (zone === "UTC") {
      return d.toISOString().replace(/\.\d{3}Z$/, "Z");
    }

    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: zone,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
      .formatToParts(d)
      .reduce<Record<string, string>>((acc, p) => {
        if (p.type !== "literal") acc[p.type] = p.value;
        return acc;
      }, {});

    // Some engines emit '24' for midnight — normalize to '00'.
    const hh = parts["hour"] === "24" ? "00" : parts["hour"];
    const local = `${parts["year"]}-${parts["month"]}-${parts["day"]}T${hh}:${parts["minute"]}:${parts["second"]}`;

    // Signed offset in minutes for this zone at this specific instant
    // (correctly handles DST transitions per-trade).
    const asUtc = Date.UTC(
      Number(parts["year"]),
      Number(parts["month"]) - 1,
      Number(parts["day"]),
      Number(hh),
      Number(parts["minute"]),
      Number(parts["second"]),
    );
    const offsetMinutes = Math.round((asUtc - d.getTime()) / 60000);
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const abs = Math.abs(offsetMinutes);
    const offH = String(Math.floor(abs / 60)).padStart(2, "0");
    const offM = String(abs % 60).padStart(2, "0");

    return `${local}${sign}${offH}:${offM}`;
  }

  tradeIndicatorEntries(trade: EngineTrade): { key: string; value: number }[] {
    return Object.entries(trade.indicators).map(([key, value]) => ({ key, value }));
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
  // CSV download
  // ------------------------------------------------------------------
  downloadTradesCsv(): void {
    const r = this.result();
    if (!r) return;
    const header = '#,Entry Time,Entry,Exit Time,Exit,PnL (pts),PnL %,Result,Signal';
    const rows = r.trades.map(t => [
      t.trade_number,
      this.formatTradeTime(t.entry_time),
      t.entry_price.toFixed(2),
      this.formatTradeTime(t.exit_time),
      t.exit_price.toFixed(2),
      t.pnl_pts.toFixed(4),
      (t.pnl_pct * 100).toFixed(4) + '%',
      t.result,
      `"${t.signal_reason}"`,
    ].join(','));
    const csv = [header, ...rows].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${this.effectiveSymbol()}_engine_trades.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // ------------------------------------------------------------------
  // History → load past study into Results tab
  // ------------------------------------------------------------------
  async onStudySelected(studyId: number): Promise<void> {
    const backendBase = (environment.backendUrl ?? 'http://localhost:5000').replace(/\/graphql$/, '');
    try {
      const detail = await firstValueFrom(
        this.http.get<any>(`${backendBase}/api/studies/${studyId}`)
      );
      // Parse LEAN statistics from JSON blob if present
      let leanStats = null;
      if (detail.leanStatisticsJson) {
        try { leanStats = JSON.parse(detail.leanStatisticsJson); } catch { /* invalid JSON, keep null */ }
      }
      // Construct an EngineBacktestResponse-shaped object for the results component
      this.result.set({
        success: true,
        strategy_name: detail.strategyName,
        fill_mode: detail.fillMode,
        initial_cash: detail.initialCash,
        final_equity: detail.finalEquity,
        net_profit: detail.totalPnL,
        total_fees: detail.totalFees ?? 0,
        total_trades: detail.totalTrades,
        winning_trades: detail.winningTrades,
        losing_trades: detail.losingTrades,
        win_rate: detail.winRate,
        statistics: {
          max_drawdown_pct: detail.maxDrawdown,
          sharpe_ratio: detail.sharpeRatio,
          sortino_ratio: detail.sortinoRatio,
          profit_factor: detail.profitFactor,
        },
        lean_statistics: leanStats,
        trades: (detail.trades ?? []).map((t: StudyTradeApiItem, i: number) =>
          mapStudyTradeToEngineTrade(t, i),
        ),
        log_lines: [],
        equity_curve: [],
        chart_bars: [],
        insights: [],
        insight_summary: {},
      });
      this.activeTab.set("1"); // Switch to Results tab
    } catch (err: any) {
      this.runError.set(err?.message ?? 'Failed to load study');
    }
  }

  availabilitySourceChips(av: DataAvailability): { label: string; days: number }[] {
    return Object.entries(av.sources || {}).map(([label, days]) => ({
      label,
      days: Array.isArray(days) ? days.length : 0,
    }));
  }
}
