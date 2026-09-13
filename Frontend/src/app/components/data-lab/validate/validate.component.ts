import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import { DataLabWorkspaceStore } from '../data-lab-workspace-store';
import { utcMsToIsoDate } from '../data-lab-request-mapper';

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
 * Data Lab Validate (PRD §7.5) — one readable column: pandas-ta CSV vs
 * TradingView CSV comparison, report display/download, and focused quality
 * evidence. The past-chain inspector stays under the Build dataset options
 * companion (PRD §7.5) — linked from here.
 */
@Component({
  selector: 'app-data-lab-validate',
  imports: [RouterLink],
  templateUrl: './validate.component.html',
  styleUrls: ['./validate.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ValidateComponent {
  private readonly http = inject(HttpClient);
  readonly store = inject(DataLabWorkspaceStore);

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
    const window = this.store.committedWindow();
    return window
      ? `${this.store.committedTicker() || '—'} · ${utcMsToIsoDate(window.startMsUtc)} → ${utcMsToIsoDate(window.endMsUtc)}`
      : 'No committed scope — quality analysis uses the ticker above with no date filter.';
  });

  async runQualityAnalysis(): Promise<void> {
    this.qualityLoading.set(true);
    this.qualityError.set('');
    this.qualityResult.set(null);
    try {
      const window = this.store.committedWindow();
      const res = await firstValueFrom(
        this.http.post<QualityAnalyzeResponse>(
          `${environment.pythonServiceUrl}/api/data-quality/analyze`,
          {
            symbol: this.ticker(),
            ...(window
              ? {
                  from_date: utcMsToIsoDate(window.startMsUtc),
                  to_date: utcMsToIsoDate(window.endMsUtc),
                }
              : {}),
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

  /** Ticker the validation requests are labeled with — defaults to the
   *  committed workspace ticker; editable for one-off comparisons. */
  readonly ticker = computed(() => this.store.committedTicker() || 'SPY');

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
      formData.append('ticker', this.ticker());

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
    this.downloadBlob(blob, `${this.ticker()}_validation_report.md`);
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
