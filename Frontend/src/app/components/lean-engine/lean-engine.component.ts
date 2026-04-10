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
}

type EngineResolution = "minute" | "daily";

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
  trades: EngineTrade[];
  log_lines: string[];
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
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, ButtonModule ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./lean-engine.component.html",
  styleUrls: ["./lean-engine.component.scss"],
})
export class LeanEngineComponent implements OnInit {
  private http = inject(HttpClient);
  private readonly apiBase = `${environment.pythonServiceUrl}/api/engine`;

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
  readonly rangePresets: ReadonlyArray<{ label: string; days: number }> = [
    { label: "1D", days: 1 },
    { label: "7D", days: 7 },
    { label: "15D", days: 15 },
    { label: "1M", days: 30 },
    { label: "3M", days: 90 },
    { label: "6M", days: 180 },
    { label: "12M", days: 365 },
    { label: "2Y", days: 730 },
  ];

  readonly running = signal(false);
  readonly result = signal<EngineBacktestResponse | null>(null);
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
    return Object.entries(schema.properties).map(([name, prop]) => ({
      name,
      prop,
    }));
  });

  constructor() {
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
  // Run a backtest
  // ------------------------------------------------------------------
  async run(): Promise<void> {
    const name = this.selectedStrategyName();
    if (!name) return;

    this.running.set(true);
    this.runError.set(null);
    this.result.set(null);

    const body: Record<string, unknown> = {
      strategy_name: name,
      fill_mode: this.fillMode(),
      initial_cash: this.initialCash(),
      params: this.paramValues(),
      auto_fetch: this.autoFetch(),
      resolution: this.resolution(),
    };
    if (this.startDate()) body["start_date"] = this.startDate();
    if (this.endDate()) body["end_date"] = this.endDate();

    try {
      const url = `${this.apiBase}/backtest`;
      const response = await firstValueFrom(
        this.http.post<EngineBacktestResponse>(url, body)
      );
      this.result.set(response);
      if (response.error) {
        this.runError.set(response.error);
      }
    } catch (err: any) {
      const detail = err?.error?.detail;
      this.runError.set(
        typeof detail === "string"
          ? detail
          : err?.message ?? "Backtest request failed"
      );
    } finally {
      this.running.set(false);
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

  tradeIndicatorEntries(trade: EngineTrade): Array<{ key: string; value: number }> {
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
    this.startDate.set(LeanEngineComponent.toIso(start));
    this.endDate.set(LeanEngineComponent.toIso(end));
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

  private static defaultEnd(): string {
    return LeanEngineComponent.toIso(LeanEngineComponent.yesterday());
  }

  private static defaultStart(): string {
    const end = LeanEngineComponent.yesterday();
    const start = new Date(end);
    start.setDate(start.getDate() - 30);
    return LeanEngineComponent.toIso(start);
  }

  // ------------------------------------------------------------------
  // CSV export — download the trade table as currently rendered. Keys
  // align 1:1 with the visible columns so a copy/paste into Excel or a
  // notebook is friction-free. Indicators are serialized as "k=v;k=v"
  // in a single cell because their shape varies per strategy.
  // ------------------------------------------------------------------
  downloadTradesCsv(): void {
    const res = this.result();
    if (!res || res.trades.length === 0) return;
    const header = [
      "trade_number",
      "entry_time",
      "entry_price",
      "exit_time",
      "exit_price",
      "pnl_pts",
      "pnl_pct",
      "result",
      "signal_reason",
      "indicators",
    ];
    const rows = res.trades.map((t) => [
      String(t.trade_number),
      this.formatTradeTime(t.entry_time),
      String(t.entry_price),
      this.formatTradeTime(t.exit_time),
      String(t.exit_price),
      String(t.pnl_pts),
      String(t.pnl_pct),
      t.result,
      t.signal_reason ?? "",
      Object.entries(t.indicators)
        .map(([k, v]) => `${k}=${v}`)
        .join(";"),
    ]);
    const csv = [header, ...rows]
      .map((row) => row.map((cell) => LeanEngineComponent.csvEscape(cell)).join(","))
      .join("\n");

    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const safeName = res.strategy_name.replace(/[^a-z0-9_-]/gi, "_");
    link.href = url;
    link.download = `trades_${safeName}_${this.startDate()}_${this.endDate()}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  private static csvEscape(value: string): string {
    if (value == null) return "";
    if (/[",\n]/.test(value)) {
      return `"${value.replace(/"/g, '""')}"`;
    }
    return value;
  }

  /** Condense the availability report's per-root counts into short chips
   *  the UI can render: "reference: 42 days" / "cache: 3 days". Filters
   *  out empty roots so SPY runs don't get a noisy empty-cache line. */
  availabilitySourceChips(
    report: DataAvailability
  ): Array<{ label: string; days: number }> {
    return Object.entries(report.sources)
      .filter(([, dates]) => dates.length > 0)
      .map(([root, dates]) => ({
        label: this.rootDisplayName(root),
        days: dates.length,
      }));
  }

  private rootDisplayName(root: string): string {
    // /lean-data → "reference mount", /lean-cache → "Polygon cache".
    // Anything else falls back to the path tail so local dev setups with
    // different layouts still render something meaningful.
    if (root.endsWith("/lean-data") || root.endsWith("\\lean-data")) {
      return "reference mount";
    }
    if (root.endsWith("/lean-cache") || root.endsWith("\\lean-cache")) {
      return "Polygon cache";
    }
    const parts = root.split(/[\\/]/);
    return parts[parts.length - 1] || root;
  }
}
