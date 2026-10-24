import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';

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

const METRICS: readonly (readonly [string, keyof QualityReportSummary])[] = [
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

/**
 * Quality evidence for the committed window: the raw bars against the cleaned
 * ones, step by step, as Python's cleanup pipeline reports it
 * (`POST /api/data-quality/analyze`). It fetches the window's bars from the
 * provider, so it runs only when asked: opening the Data tab never starts it.
 */
@Component({
  selector: 'app-quality-evidence',
  templateUrl: './quality-evidence.component.html',
  styleUrl: './quality-evidence.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class QualityEvidenceComponent {
  private readonly http = inject(HttpClient);

  readonly symbol = input.required<string>();
  /** `YYYY-MM-DD`, the committed window the report is bounded by. */
  readonly from = input.required<string>();
  readonly to = input.required<string>();

  readonly loading = signal(false);
  readonly error = signal('');
  readonly result = signal<QualityAnalyzeResponse | null>(null);

  /** Raw-vs-clean rows, rendered from the server's numbers only. */
  readonly metricRows = computed(() => {
    const r = this.result();
    if (!r) return [];
    return METRICS.map(([label, key]) => ({ label, raw: r.raw_summary[key], clean: r.clean_summary[key] }));
  });

  async run(): Promise<void> {
    if (this.loading()) return;
    this.loading.set(true);
    this.error.set('');
    this.result.set(null);
    try {
      this.result.set(
        await firstValueFrom(
          this.http.post<QualityAnalyzeResponse>(`${environment.pythonServiceUrl}/api/data-quality/analyze`, {
            symbol: this.symbol(),
            from_date: this.from(),
            to_date: this.to(),
            volume_fix: 'round',
            recompute_indicators: false,
            indicator_entries: [],
          }),
        ),
      );
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loading.set(false);
    }
  }

  download(): void {
    const r = this.result();
    if (!r) return;
    const url = URL.createObjectURL(new Blob([this.markdown(r)], { type: 'text/markdown' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `${r.ticker}_quality_report.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  private markdown(r: QualityAnalyzeResponse): string {
    const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const lines: string[] = [];
    lines.push(`# Data Quality Report — ${r.ticker}`);
    lines.push('');
    lines.push(`**Range:** ${r.from_date} → ${r.to_date}  **Generated:** ${now}`);
    lines.push('');
    lines.push('| Metric | Raw | Clean |');
    lines.push('|---|---:|---:|');
    for (const row of this.metricRows()) {
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
}
