import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
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
}

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

  readonly fillMode = signal<"signal_bar_close" | "next_bar_open">("signal_bar_close");
  readonly startDate = signal<string>("");
  readonly endDate = signal<string>("");
  readonly initialCash = signal<number>(100000);

  readonly running = signal(false);
  readonly result = signal<EngineBacktestResponse | null>(null);
  readonly runError = signal<string | null>(null);

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

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------
  ngOnInit(): void {
    this.loadStrategies();
  }

  async loadStrategies(): Promise<void> {
    this.strategiesLoading.set(true);
    this.strategiesError.set(null);
    try {
      const url = `${this.apiBase}/strategies`;
      const list = await firstValueFrom(this.http.get<StrategyInfo[]>(url));
      this.strategies.set(list);
      // Auto-select the first strategy so the form has something to render.
      if (list.length > 0) {
        this.onStrategyChange(list[0].name);
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
}
