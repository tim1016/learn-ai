import {
  Component, signal, computed, inject,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { TableModule } from 'primeng/table';
import { Tag } from 'primeng/tag';
import { Accordion, AccordionPanel, AccordionHeader, AccordionContent } from 'primeng/accordion';
import { environment } from '../../../environments/environment';
import {
  StrategyLabChartComponent,
} from '../strategy-lab/strategy-lab-chart/strategy-lab-chart.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import type {
  ChartBar, ChartIndicatorResult, BacktestTradeForChart,
} from '../strategy-lab/strategy-lab-chart/strategy-lab-chart.component';

// ── Response types ──

interface ValidationTradeResponse {
  trade_number: number;
  trade_type: string;
  entry_timestamp: string;
  exit_timestamp: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  cumulative_pnl_pct: number;
  signal_reason: string;
  indicator_snapshot: Record<string, number | null>;
}

interface ReferenceSummary {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  total_pnl_pct: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  profit_factor: number;
}

interface TradeComparisonResponse {
  trade_num: number;
  ref_entry_time: string | null;
  our_entry_time: string | null;
  ref_exit_time: string | null;
  our_exit_time: string | null;
  ref_entry_price: number | null;
  our_entry_price: number | null;
  ref_exit_price: number | null;
  our_exit_price: number | null;
  ref_pnl: number | null;
  our_pnl: number | null;
  ref_pnl_pct: number | null;
  our_pnl_pct: number | null;
  entry_price_delta: number | null;
  exit_price_delta: number | null;
  pnl_delta: number | null;
  pnl_pct_delta: number | null;
  timestamp_delta_s: number | null;
  matched: boolean;
  source: string;
}

interface MatchStatsResponse {
  total_ref: number;
  total_ours: number;
  matched_count: number;
  extra_ref: number;
  extra_ours: number;
  match_rate: number;
  avg_ts_delta_s: number;
  avg_entry_price_delta: number;
  avg_pnl_delta: number;
}

interface ColumnDescription {
  name: string;
  description: string;
  type: string;
}

interface BarTableRow {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  [key: string]: number | string;
}

interface ValidationStudyResult {
  success: boolean;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  win_loss_ratio: number;
  profit_factor: number;
  expectancy_per_trade: number;
  total_pnl_pct: number;
  total_pnl_pts: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  bars_processed: number;
  trades: ValidationTradeResponse[];
  reference_summary: ReferenceSummary | null;
  comparisons: TradeComparisonResponse[];
  match_stats: MatchStatsResponse | null;
  source_bars: number;
  rth_bars: number;
  resampled_bars: number;
  chart_bars: ChartBar[];
  chart_indicators: ChartIndicatorResult[];
  bar_table: BarTableRow[];
  column_descriptions: ColumnDescription[];
  parameters: Record<string, unknown>;
  error: string | null;
}

interface RefTrade {
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  result: string;
}

@Component({
  selector: 'app-strategy-lab-validation',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    TableModule, Tag,
    Accordion, AccordionPanel, AccordionHeader, AccordionContent,
    StrategyLabChartComponent,
    PageHeaderComponent,
  ],
  templateUrl: './strategy-lab-validation.component.html',
  styleUrls: ['./strategy-lab-validation.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabValidationComponent {
  private http = inject(HttpClient);
  private baseUrl = `${environment.pythonServiceUrl}/api/validation-study`;

  // File state
  minuteCsvFile = signal<File | null>(null);
  minuteFileName = signal<string | null>(null);
  minuteBarCount = signal(0);
  referenceFileName = signal<string | null>(null);
  referenceTrades = signal<RefTrade[]>([]);

  // Result state
  loading = signal(false);
  error = signal<string | null>(null);
  result = signal<ValidationStudyResult | null>(null);
  markdownReport = signal('');
  reportLoading = signal(false);
  exportLoading = signal(false);

  // Computed
  chartBars = computed(() => this.result()?.chart_bars ?? []);
  chartIndicators = computed(() => this.result()?.chart_indicators ?? []);

  tradesForChart = computed<BacktestTradeForChart[]>(() => {
    const r = this.result();
    if (!r) return [];
    return r.trades.map(t => ({
      entry_timestamp: t.entry_timestamp,
      exit_timestamp: t.exit_timestamp,
      entry_price: t.entry_price,
      exit_price: t.exit_price,
      pnl: t.pnl,
      trade_type: t.trade_type,
      signal_reason: t.signal_reason,
    }));
  });

  comparisons = computed(() => this.result()?.comparisons ?? []);
  matchStats = computed(() => this.result()?.match_stats ?? null);
  barTable = computed(() => this.result()?.bar_table ?? []);
  columnDescriptions = computed(() => this.result()?.column_descriptions ?? []);

  get barTableColumns(): string[] {
    const table = this.barTable();
    if (!table.length) return [];
    return Object.keys(table[0]);
  }

  // ── File handlers ──

  onMinuteCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (!input.files?.length) return;
    const file = input.files[0];
    this.minuteCsvFile.set(file);
    this.minuteFileName.set(file.name);

    const reader = new FileReader();
    reader.onload = () => {
      const text = reader.result as string;
      const lines = text.trim().split('\n');
      this.minuteBarCount.set(Math.max(0, lines.length - 1));
    };
    reader.readAsText(file);
  }

  onReferenceTradesSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (!input.files?.length) return;
    const file = input.files[0];
    this.referenceFileName.set(file.name);

    const reader = new FileReader();
    reader.onload = () => {
      const text = reader.result as string;
      this.parseReferenceTradesCsv(text);
    };
    reader.readAsText(file);
  }

  private parseReferenceTradesCsv(csv: string): void {
    const lines = csv.trim().split('\n');
    if (lines.length < 2) return;

    const trades: RefTrade[] = [];
    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(',');
      if (cols.length < 6) continue;

      const pnl = parseFloat(cols[4]);
      const entryPrice = parseFloat(cols[2]);
      if (isNaN(pnl) || isNaN(entryPrice) || entryPrice === 0) continue;

      trades.push({
        entry_time: cols[0].trim(),
        exit_time: cols[1].trim(),
        entry_price: entryPrice,
        exit_price: parseFloat(cols[3]),
        pnl,
        pnl_pct: cols.length > 5 ? parseFloat(cols[5]) : pnl / entryPrice,
        result: pnl > 0 ? 'WIN' : 'LOSS',
      });
    }
    this.referenceTrades.set(trades);
  }

  // ── API calls ──

  async runValidation(): Promise<void> {
    const file = this.minuteCsvFile();
    if (!file) {
      this.error.set('Upload a minute CSV file first');
      return;
    }

    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);
    this.markdownReport.set('');

    const formData = new FormData();
    formData.append('minute_csv', file);
    formData.append('reference_trades_json', JSON.stringify(this.referenceTrades()));
    formData.append('parameters', JSON.stringify({}));

    try {
      const res = await firstValueFrom(
        this.http.post<ValidationStudyResult>(`${this.baseUrl}/run`, formData),
      );
      if (!res.success) {
        this.error.set(res.error || 'Validation failed');
      } else {
        this.result.set(res);
      }
    } catch (err: any) {
      this.error.set(err?.error?.detail || err?.message || 'Request failed');
    } finally {
      this.loading.set(false);
    }
  }

  async downloadCsvExport(): Promise<void> {
    const file = this.minuteCsvFile();
    if (!file) return;

    this.exportLoading.set(true);
    const formData = new FormData();
    formData.append('minute_csv', file);
    formData.append('reference_trades_json', JSON.stringify(this.referenceTrades()));
    formData.append('parameters', JSON.stringify({}));

    try {
      const blob = await firstValueFrom(
        this.http.post(`${this.baseUrl}/export-csv`, formData, { responseType: 'blob' }),
      );
      this.downloadBlob(blob, 'validation_study_SPY_15m.zip');
    } catch (err: any) {
      this.error.set(err?.error?.detail || err?.message || 'Export failed');
    } finally {
      this.exportLoading.set(false);
    }
  }

  async generateReport(): Promise<void> {
    const file = this.minuteCsvFile();
    if (!file) return;

    this.reportLoading.set(true);
    const formData = new FormData();
    formData.append('minute_csv', file);
    formData.append('reference_trades_json', JSON.stringify(this.referenceTrades()));
    formData.append('parameters', JSON.stringify({}));

    try {
      const res = await firstValueFrom(
        this.http.post<{ markdown: string }>(`${this.baseUrl}/report`, formData),
      );
      this.markdownReport.set(res.markdown);
    } catch (err: any) {
      this.error.set(err?.error?.detail || err?.message || 'Report generation failed');
    } finally {
      this.reportLoading.set(false);
    }
  }

  pdfLoading = signal(false);

  async downloadPdfReport(): Promise<void> {
    const file = this.minuteCsvFile();
    if (!file) return;

    this.pdfLoading.set(true);
    const formData = new FormData();
    formData.append('minute_csv', file);
    formData.append('reference_trades_json', JSON.stringify(this.referenceTrades()));
    formData.append('parameters', JSON.stringify({}));

    try {
      const blob = await firstValueFrom(
        this.http.post(`${this.baseUrl}/report-pdf`, formData, { responseType: 'blob' }),
      );
      this.downloadBlob(blob, 'validation_report_SPY_15m.pdf');
    } catch (err: any) {
      this.error.set(err?.error?.detail || err?.message || 'PDF generation failed');
    } finally {
      this.pdfLoading.set(false);
    }
  }

  copyReport(): void {
    navigator.clipboard.writeText(this.markdownReport());
  }

  // ── Formatting helpers ──

  formatPrice(val: number | null | undefined): string {
    return val != null ? val.toFixed(2) : '-';
  }

  formatPct(val: number | null | undefined): string {
    if (val == null) return '-';
    return (val * 100).toFixed(2) + '%';
  }

  formatTimestamp(ts: number): string {
    return new Date(ts).toISOString().replace('T', ' ').slice(0, 16);
  }

  matchSeverity(matched: boolean): 'success' | 'danger' | 'warn' {
    return matched ? 'success' : 'danger';
  }

  sourceSeverity(source: string): 'success' | 'danger' | 'warn' {
    if (source === 'matched') return 'success';
    if (source === 'extra_ref') return 'warn';
    return 'danger';
  }

  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}
