import {
  Component, signal, computed, inject,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../services/market-data.service';
import { IndicatorTableRow } from '../../graphql/types';

interface CsvRow {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  bb_basis: number | null;
  bb_upper: number | null;
  bb_lower: number | null;
  supertrend_up: number | null;
  supertrend_down: number | null;
  ema_5: number | null;
  ema_10: number | null;
  ema_20: number | null;
  ema_30: number | null;
  ema_40: number | null;
  ema_50: number | null;
  ema_100: number | null;
  ema_200: number | null;
  rsi: number | null;
  rsi_ma: number | null;
  macd: number | null;
  macd_signal: number | null;
  macd_histogram: number | null;
  adx: number | null;
  [key: string]: string | number | null;
}

interface ComparisonRow {
  time: string;
  field: string;
  csvValue: number | null;
  ourValue: number | null;
  diff: number | null;
  pctDiff: number | null;
}

const TV_CSV_MA_MAPPING = [
  // First MA group: columns 11-14 → EMA 5, 10, 20, 30
  { csvIndex: 10, emaKey: 'ema_5' },
  { csvIndex: 11, emaKey: 'ema_10' },
  { csvIndex: 12, emaKey: 'ema_20' },
  { csvIndex: 13, emaKey: 'ema_30' },
  // Second MA group: columns 15-18 → EMA 40, 50, 100, 200
  { csvIndex: 14, emaKey: 'ema_40' },
  { csvIndex: 15, emaKey: 'ema_50' },
  { csvIndex: 16, emaKey: 'ema_100' },
  { csvIndex: 17, emaKey: 'ema_200' },
];

@Component({
  selector: 'app-indicator-validation',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule],
  templateUrl: './indicator-validation.component.html',
  styleUrls: ['./indicator-validation.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class IndicatorValidationComponent {
  private marketData = inject(MarketDataService);

  ticker = signal('SPY');
  fromDate = signal('2026-03-26');
  toDate = signal('2026-03-27');
  loading = signal(false);
  error = signal('');

  csvRows = signal<CsvRow[]>([]);
  ourRows = signal<IndicatorTableRow[]>([]);
  ourColumns = signal<string[]>([]);

  activeTab = signal<'table' | 'comparison' | 'csv'>('table');

  // Display page for large datasets
  page = signal(0);
  pageSize = signal(50);

  csvFileName = signal('');

  comparisonFields = signal<string[]>([
    'close', 'bb_basis', 'bb_upper', 'bb_lower',
    'supertrend_up', 'supertrend_down',
    'ema_5', 'ema_10', 'ema_20', 'ema_30',
    'ema_40', 'ema_50', 'ema_100', 'ema_200',
    'rsi', 'rsi_ma', 'macd', 'macd_signal', 'macd_histogram', 'adx',
  ]);

  selectedField = signal('ema_5');

  get totalPages(): number {
    return Math.ceil(this.ourRows().length / this.pageSize());
  }

  pagedOurRows = computed(() => {
    const start = this.page() * this.pageSize();
    return this.ourRows().slice(start, start + this.pageSize());
  });

  pagedCsvRows = computed(() => {
    const start = this.page() * this.pageSize();
    return this.csvRows().slice(start, start + this.pageSize());
  });

  comparisonRows = computed(() => {
    const csv = this.csvRows();
    const ours = this.ourRows();
    const field = this.selectedField();

    if (!csv.length || !ours.length) return [];

    const rows: ComparisonRow[] = [];
    const start = this.page() * this.pageSize();
    const end = Math.min(start + this.pageSize(), ours.length);

    for (let i = start; i < end; i++) {
      const ourRow = ours[i];
      const csvRow = csv[i];
      if (!ourRow || !csvRow) continue;

      const csvVal = (csvRow as Record<string, unknown>)[field] as number | null;
      const ourVal = (ourRow as Record<string, unknown>)[field] as number | null;

      let diff: number | null = null;
      let pctDiff: number | null = null;
      if (csvVal != null && ourVal != null) {
        diff = ourVal - csvVal;
        pctDiff = csvVal !== 0 ? (diff / Math.abs(csvVal)) * 100 : null;
      }

      rows.push({
        time: csvRow.time || this.formatTimestamp(ourRow.time),
        field,
        csvValue: csvVal,
        ourValue: ourVal,
        diff,
        pctDiff,
      });
    }
    return rows;
  });

  summaryStats = computed(() => {
    const csv = this.csvRows();
    const ours = this.ourRows();
    const field = this.selectedField();

    if (!csv.length || !ours.length) return null;

    let totalDiffs = 0;
    let sumAbsDiff = 0;
    let maxAbsDiff = 0;
    let count = 0;

    for (let i = 0; i < Math.min(csv.length, ours.length); i++) {
      const csvVal = (csv[i] as Record<string, unknown>)[field] as number | null;
      const ourVal = (ours[i] as Record<string, unknown>)[field] as number | null;
      if (csvVal != null && ourVal != null) {
        const d = Math.abs(ourVal - csvVal);
        sumAbsDiff += d;
        maxAbsDiff = Math.max(maxAbsDiff, d);
        totalDiffs++;
        count++;
      }
    }

    return {
      field,
      count,
      meanAbsDiff: count > 0 ? sumAbsDiff / count : 0,
      maxAbsDiff,
    };
  });

  onCsvUpload(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    this.csvFileName.set(file.name);

    const reader = new FileReader();
    reader.onload = () => {
      const text = reader.result as string;
      this.parseTradingViewCsv(text);
    };
    reader.readAsText(file);
  }

  private parseTradingViewCsv(text: string): void {
    const lines = text.trim().split('\n');
    if (lines.length < 2) return;

    const rows: CsvRow[] = [];

    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(',');
      if (cols.length < 29) continue;

      const parseNum = (val: string): number | null => {
        if (!val || val.trim() === '') return null;
        const n = parseFloat(val);
        return isNaN(n) ? null : n;
      };

      rows.push({
        time: cols[0],
        open: parseFloat(cols[1]),
        high: parseFloat(cols[2]),
        low: parseFloat(cols[3]),
        close: parseFloat(cols[4]),
        bb_basis: parseNum(cols[5]),
        bb_upper: parseNum(cols[6]),
        bb_lower: parseNum(cols[7]),
        supertrend_up: parseNum(cols[8]),
        supertrend_down: parseNum(cols[9]),
        // First MA group → EMA 5, 10, 20, 30
        ema_5: parseNum(cols[10]),
        ema_10: parseNum(cols[11]),
        ema_20: parseNum(cols[12]),
        ema_30: parseNum(cols[13]),
        // Second MA group → EMA 40, 50, 100, 200
        ema_40: parseNum(cols[14]),
        ema_50: parseNum(cols[15]),
        ema_100: parseNum(cols[16]),
        ema_200: parseNum(cols[17]),
        volume: parseFloat(cols[18]),
        rsi: parseNum(cols[19]),
        rsi_ma: parseNum(cols[20]),
        // Skip cols 21-24 (RSI divergence labels)
        macd_histogram: parseNum(cols[25]),
        macd: parseNum(cols[26]),
        macd_signal: parseNum(cols[27]),
        adx: parseNum(cols[28]),
      });
    }

    this.csvRows.set(rows);
  }

  async generate(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    this.page.set(0);

    try {
      const result = await firstValueFrom(
        this.marketData.generateIndicatorTable(
          this.ticker(),
          this.fromDate(),
          this.toDate()
        )
      );

      if (!result.success) {
        this.error.set(result.error ?? 'Unknown error');
        return;
      }

      const parsed: IndicatorTableRow[] = result.rows.map(jsonStr => JSON.parse(jsonStr));
      this.ourRows.set(parsed);
      this.ourColumns.set(result.columns);
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loading.set(false);
    }
  }

  formatTimestamp(ts: number): string {
    return new Date(ts).toISOString().replace('T', ' ').slice(0, 19);
  }

  formatNum(val: number | null | undefined): string {
    if (val == null) return '—';
    return val.toFixed(6);
  }

  prevPage(): void {
    if (this.page() > 0) this.page.update(p => p - 1);
  }

  nextPage(): void {
    if (this.page() < this.totalPages - 1) this.page.update(p => p + 1);
  }

  getDiffClass(pctDiff: number | null): string {
    if (pctDiff == null) return '';
    const abs = Math.abs(pctDiff);
    if (abs < 0.001) return 'match-exact';
    if (abs < 0.01) return 'match-close';
    if (abs < 0.1) return 'match-ok';
    return 'match-bad';
  }
}
