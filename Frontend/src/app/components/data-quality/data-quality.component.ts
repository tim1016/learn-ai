import {
  Component,
  signal,
  computed,
  inject,
  ChangeDetectionStrategy,
} from "@angular/core";
import { CommonModule } from "@angular/common";
import { FormsModule } from "@angular/forms";
import { RouterModule } from "@angular/router";
import { HttpClient } from "@angular/common/http";
import { firstValueFrom } from "rxjs";
import { environment } from "../../../environments/environment";

interface StepReport {
  order: number;
  name: string;
  library: string;
  description: string;
  bars_before: number;
  bars_after: number;
  bars_removed: number;
  details: Record<string, any>;
}

interface Summary {
  total_bars: number;
  trading_days: number;
  bars_per_day_distribution: Record<string, number>;
  date_range: string[];
  zero_volume_bars: number;
  flat_bars_ohlc_equal: number;
  flat_with_volume: number;
  fractional_volume_bars: number;
  fractional_volume_date_range: string[];
  vwap_above_high: number;
  vwap_below_low: number;
  ohlc_violations: number;
  duplicate_timestamps: number;
  weekend_bars: number;
  intraday_gaps: number;
  big_moves_1pct: number;
  big_moves_2pct: number;
  indicators_recomputed?: boolean;
}

interface AnalyzeResponse {
  success: boolean;
  ticker: string;
  from_date: string;
  to_date: string;
  raw_summary: Summary;
  clean_summary: Summary;
  steps: StepReport[];
  raw_data_token: string;
  clean_data_token: string;
}

interface MetricRow {
  label: string;
  raw: number | string;
  clean: number | string;
  delta: number | string;
  improved: boolean;
  neutral: boolean;
}

@Component({
  selector: "app-data-quality",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, RouterModule],
  templateUrl: "./data-quality.component.html",
  styleUrl: "./data-quality.component.scss",
})
export class DataQualityComponent {
  private http = inject(HttpClient);
  private baseUrl = environment.pythonServiceUrl;

  // Form fields
  ticker = signal("DIA");
  fromDate = signal("2024-03-28");
  toDate = signal("2026-03-28");
  volumeFix = signal("round");
  recomputeIndicators = signal(false);

  // State
  loading = signal(false);
  error = signal("");
  activeTab = signal<"steps" | "comparison" | "viewer">("steps");
  viewerMode = signal<"raw" | "clean">("raw");

  // Results
  result = signal<AnalyzeResponse | null>(null);

  // Computed
  rawSummary = computed(() => this.result()?.raw_summary ?? null);
  cleanSummary = computed(() => this.result()?.clean_summary ?? null);
  steps = computed(() => this.result()?.steps ?? []);

  totalRemoved = computed(() => {
    const r = this.rawSummary();
    const c = this.cleanSummary();
    if (!r || !c) return 0;
    return r.total_bars - c.total_bars;
  });

  removalPct = computed(() => {
    const r = this.rawSummary();
    if (!r || r.total_bars === 0) return "0";
    return ((this.totalRemoved() / r.total_bars) * 100).toFixed(1);
  });

  comparisonMetrics = computed((): MetricRow[] => {
    const r = this.rawSummary();
    const c = this.cleanSummary();
    if (!r || !c) return [];

    const row = (
      label: string,
      rawVal: number | string,
      cleanVal: number | string,
      lowerIsBetter = true
    ): MetricRow => {
      const rawNum =
        typeof rawVal === "number" ? rawVal : parseInt(rawVal as string, 10);
      const cleanNum =
        typeof cleanVal === "number"
          ? cleanVal
          : parseInt(cleanVal as string, 10);
      const delta = cleanNum - rawNum;
      const improved = lowerIsBetter ? delta < 0 : delta > 0;
      const neutral = delta === 0;
      return {
        label,
        raw: rawVal,
        clean: cleanVal,
        delta: delta > 0 ? `+${delta}` : `${delta}`,
        improved,
        neutral,
      };
    };

    return [
      row("Total bars", r.total_bars, c.total_bars, false),
      row("Trading days", r.trading_days, c.trading_days, false),
      row("Zero-volume bars", r.zero_volume_bars, c.zero_volume_bars),
      row("Flat bars (O=H=L=C)", r.flat_bars_ohlc_equal, c.flat_bars_ohlc_equal),
      row("Fractional volume bars", r.fractional_volume_bars, c.fractional_volume_bars),
      row("VWAP > high violations", r.vwap_above_high, c.vwap_above_high),
      row("VWAP < low violations", r.vwap_below_low, c.vwap_below_low),
      row("OHLC violations", r.ohlc_violations, c.ohlc_violations),
      row("Duplicate timestamps", r.duplicate_timestamps, c.duplicate_timestamps),
      row("Weekend bars", r.weekend_bars, c.weekend_bars),
      row("Intraday gaps", r.intraday_gaps, c.intraday_gaps),
    ];
  });

  async runAnalysis(): Promise<void> {
    this.loading.set(true);
    this.error.set("");
    this.result.set(null);

    try {
      const payload = {
        ticker: this.ticker(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        volume_fix: this.volumeFix(),
        recompute_indicators: this.recomputeIndicators(),
        indicator_entries: [],
      };

      const res = await firstValueFrom(
        this.http.post<AnalyzeResponse>(
          `${this.baseUrl}/api/data-quality/analyze`,
          payload
        )
      );
      this.result.set(res);
      this.activeTab.set("steps");
    } catch (e: any) {
      this.error.set(e?.error?.detail || e?.message || "Analysis failed");
    } finally {
      this.loading.set(false);
    }
  }

  async downloadCsv(type: "raw" | "clean"): Promise<void> {
    const r = this.result();
    if (!r) return;

    const token =
      type === "raw" ? r.raw_data_token : r.clean_data_token;
    const endpoint =
      type === "raw" ? "raw-csv" : "clean-csv";

    try {
      const blob = await firstValueFrom(
        this.http.get(
          `${this.baseUrl}/api/data-quality/${endpoint}?token=${token}`,
          { responseType: "blob" }
        )
      );
      const filename = `${r.ticker}_${type}_${r.from_date}_to_${r.to_date}.csv`;
      this.downloadBlob(blob, filename);
    } catch (e: any) {
      this.error.set(`Download failed: ${e?.message || "unknown error"}`);
    }
  }

  stepBarWidth(step: StepReport): string {
    if (step.bars_before === 0) return "100%";
    const pct = (step.bars_after / step.bars_before) * 100;
    return `${Math.max(pct, 2)}%`;
  }

  formatNumber(val: number | string): string {
    const num = typeof val === "string" ? parseInt(val, 10) : val;
    return isNaN(num) ? String(val) : num.toLocaleString();
  }

  detailEntries(details: Record<string, any>): { key: string; value: any }[] {
    return Object.entries(details)
      .filter(([, v]) => !Array.isArray(v) || v.length <= 10)
      .map(([key, value]) => ({
        key: key.replace(/_/g, " "),
        value: Array.isArray(value) ? value.join(", ") : value,
      }));
  }

  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}
