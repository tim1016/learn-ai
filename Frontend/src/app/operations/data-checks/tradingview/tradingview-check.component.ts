import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import { ProviderBoundsService } from '../../../shared/date-picker';
import { parseIsoDate } from '../../../shared/date/iso-date';
import { TickerRangePickerComponent } from '../../../shared/ticker-range-picker/ticker-range-picker.component';
import type { TickerRange } from '../../../shared/ticker-range-picker/ticker-range-picker.types';

interface ValidationResponse {
  success: boolean;
  report: string;
}

interface QualityReportStep {
  order: number;
  name: string;
  library: string;
  description: string;
  bars_before: number;
  bars_after: number;
  bars_removed: number;
}

interface QualityReportSummary {
  total_bars: number;
  trading_days: number;
  zero_volume_bars: number;
  flat_bars_ohlc_equal: number;
  fractional_volume_bars: number;
  vwap_above_high: number;
  vwap_below_low: number;
  ohlc_violations: number;
  duplicate_timestamps: number;
  weekend_bars: number;
  intraday_gaps: number;
}

interface QualityAnalyzeResponse {
  success: boolean;
  ticker: string;
  from_date: string;
  to_date: string;
  raw_summary: QualityReportSummary;
  clean_summary: QualityReportSummary;
  steps: QualityReportStep[];
}

/**
 * Operations → Data checks → TradingView comparison — one readable column:
 * pandas-ta CSV vs TradingView CSV comparison, report display/download, and
 * focused quality evidence. The past-chain inspector stays under the Build
 * dataset options companion — linked from here.
 *
 * Its ticker and dates are its own, picked here. A link that carried them
 * (`?ticker=SPY&from=2026-01-02&to=2026-06-30`, the shape the Data Lab and
 * `/data-quality` links had) starts the picker there; the uploaded files
 * cannot travel in a link. Opening the page uploads and runs nothing.
 */
@Component({
  selector: 'app-tradingview-check',
  imports: [RouterLink, TickerRangePickerComponent],
  templateUrl: './tradingview-check.component.html',
  styleUrls: ['./tradingview-check.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TradingviewCheckComponent {
  private readonly http = inject(HttpClient);

  /** The scope a link carried, bound from its query. */
  readonly ticker = input<string>();
  readonly from = input<string>();
  readonly to = input<string>();

  /** The picker's value. A link carrying a new scope replaces it; an edit holds until then. */
  protected readonly range = linkedSignal<TickerRange>(() => ({
    symbol: (this.ticker() ?? '').trim().toUpperCase() || 'SPY',
    from: parseIsoDate(this.from() ?? '') ?? '',
    to: parseIsoDate(this.to() ?? '') ?? '',
    resolution: 'daily',
  }));
  /** Any calendar day from the provider's oldest: a check may cover the session still forming. */
  protected readonly dateBounds = inject(ProviderBoundsService).openEnded;

  readonly ourCsvFile = signal<File | null>(null);
  readonly tvCsvFile = signal<File | null>(null);
  readonly loadingValidation = signal(false);
  readonly validationError = signal('');
  readonly validationReport = signal('');

  // ── Quality evidence ──────────────────────────────────────
  readonly qualityLoading = signal(false);
  readonly qualityError = signal('');
  readonly qualityResult = signal<QualityAnalyzeResponse | null>(null);

  /** Raw-vs-clean summary rows, rendered from the server's numbers only. */
  readonly qualityMetricRows = computed(() => {
    const r = this.qualityResult();
    if (!r) return [];
    const rows: { label: string; raw: number; clean: number }[] = [];
    const labels: [string, keyof QualityReportSummary][] = [
      ['Total bars', 'total_bars'],
      ['Trading days', 'trading_days'],
      ['Zero-volume bars', 'zero_volume_bars'],
      ['Flat bars (O=H=L=C)', 'flat_bars_ohlc_equal'],
      ['Fractional volume bars', 'fractional_volume_bars'],
      ['VWAP > high', 'vwap_above_high'],
      ['VWAP < low', 'vwap_below_low'],
      ['OHLC violations', 'ohlc_violations'],
      ['Duplicate timestamps', 'duplicate_timestamps'],
      ['Weekend bars', 'weekend_bars'],
      ['Intraday gaps', 'intraday_gaps'],
    ];
    for (const [label, key] of labels) {
      rows.push({ label, raw: r.raw_summary[key], clean: r.clean_summary[key] });
    }
    return rows;
  });

  readonly qualityScopeSummary = computed(() => {
    const { symbol, from, to } = this.range();
    return from !== '' && to !== ''
      ? `${symbol || '—'} · ${from} → ${to}`
      : 'Pick both dates above before running quality analysis.';
  });

  /** Quality analysis requires from_date/to_date server-side; without both
   *  dates (a picker holds an empty date for one it cannot use) the request
   *  is a guaranteed 422, so the run action stays disabled until they exist. */
  readonly canRunQuality = computed(() => {
    const { symbol, from, to } = this.range();
    return symbol !== '' && from !== '' && to !== '' && !this.qualityLoading();
  });

  async runQualityAnalysis(): Promise<void> {
    if (!this.canRunQuality()) return;
    this.qualityLoading.set(true);
    this.qualityError.set('');
    this.qualityResult.set(null);
    try {
      const { from, to } = this.range();
      const res = await firstValueFrom(
        this.http.post<QualityAnalyzeResponse>(
          `${environment.pythonServiceUrl}/api/data-quality/analyze`,
          {
            symbol: this.label(),
            from_date: from,
            to_date: to,
            volume_fix: 'round',
            recompute_indicators: false,
            indicator_entries: [],
          },
        ),
      );
      this.qualityResult.set(res);
    } catch (e: unknown) {
      this.qualityError.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.qualityLoading.set(false);
    }
  }

  downloadQualityReport(): void {
    const r = this.qualityResult();
    if (!r) return;
    this.downloadBlob(
      new Blob([this.buildQualityMarkdown(r)], { type: 'text/markdown' }),
      `${r.ticker}_quality_report.md`,
    );
  }

  private buildQualityMarkdown(r: QualityAnalyzeResponse): string {
    const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const lines: string[] = [];
    lines.push(`# Data Quality Report — ${r.ticker}`);
    lines.push('');
    lines.push(`**Range:** ${r.from_date} → ${r.to_date}  **Generated:** ${now}`);
    lines.push('');
    lines.push('| Metric | Raw | Clean |');
    lines.push('|---|---:|---:|');
    for (const row of this.qualityMetricRows()) {
      lines.push(`| ${row.label} | ${row.raw} | ${row.clean} |`);
    }
    lines.push('');
    for (const step of r.steps) {
      lines.push(`### ${step.order}. ${step.name} (${step.library})`);
      lines.push(step.description);
      lines.push(`Bars: ${step.bars_before} → ${step.bars_after} (${step.bars_removed} removed)`);
      lines.push('');
    }
    return lines.join('\n');
  }

  /** The ticker the requests and the downloaded reports are labeled with. */
  private readonly label = computed(() => this.range().symbol || 'SPY');

  readonly canRunValidation = computed(
    () => !!this.ourCsvFile() && !!this.tvCsvFile() && !this.loadingValidation(),
  );

  onOurCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.ourCsvFile.set(input.files?.[0] ?? null);
  }

  onTvCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.tvCsvFile.set(input.files?.[0] ?? null);
  }

  async runValidation(): Promise<void> {
    const ourFile = this.ourCsvFile();
    const tvFile = this.tvCsvFile();
    if (!ourFile || !tvFile) return;

    this.loadingValidation.set(true);
    this.validationError.set('');
    this.validationReport.set('');

    try {
      const formData = new FormData();
      formData.append('our_csv', ourFile);
      formData.append('tv_csv', tvFile);
      formData.append('ticker', this.label());

      const response = await firstValueFrom(
        this.http.post<ValidationResponse>(
          `${environment.pythonServiceUrl}/api/dataset/validation-report`,
          formData,
        ),
      );

      if (response.success) {
        this.validationReport.set(response.report);
      } else {
        this.validationError.set('Validation report generation failed');
      }
    } catch (e: unknown) {
      this.validationError.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loadingValidation.set(false);
    }
  }

  downloadValidationReport(): void {
    const report = this.validationReport();
    if (!report) return;
    const blob = new Blob([report], { type: 'text/markdown' });
    this.downloadBlob(blob, `${this.label()}_validation_report.md`);
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
