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
  imports: [CommonModule, FormsModule, RouterModule],
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
  readonly startDate = signal<string>("");
  readonly endDate = signal<string>("");
  readonly initialCash = signal<number>(100000);

  readonly running = signal(false);
  readonly result = signal<EngineBacktestResponse | null>(null);
  readonly runError = signal<string | null>(null);

  // ------------------------------------------------------------------
  // Dynamic data layer state — availability + on-demand fetch
  // ------------------------------------------------------------------
  readonly autoFetch = signal<boolean>(false);
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

  tradeIndicatorEntries(trade: EngineTrade): Array<{ key: string; value: number }> {
    return Object.entries(trade.indicators).map(([key, value]) => ({ key, value }));
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
