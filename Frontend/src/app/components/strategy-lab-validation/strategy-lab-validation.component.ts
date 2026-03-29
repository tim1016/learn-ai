import {
  Component, signal, computed, inject,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { MarketDataService } from '../../services/market-data.service';
import { BacktestResult } from '../../graphql/types';
import { validateDateRange, getMinAllowedDate } from '../../utils/date-validation';

interface ExternalTrade {
  tradeNum: number;
  type: string;
  entryTime: string;
  exitTime: string;
  entryPrice: number;
  exitPrice: number;
  pnl: number;
  cumulativePnl: number;
  signal: string;
}

interface CsvBar {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface TradeComparison {
  tradeNum: number;
  externalType: string;
  ourType: string;
  externalEntryTime: string;
  ourEntryTime: string;
  externalExitTime: string;
  ourExitTime: string;
  externalEntryPrice: number;
  ourEntryPrice: number;
  externalExitPrice: number;
  ourExitPrice: number;
  externalPnl: number;
  ourPnl: number;
  pnlDiff: number;
  matched: boolean;
}

export type DataSource = 'polygon' | 'csv';

@Component({
  selector: 'app-strategy-lab-validation',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './strategy-lab-validation.component.html',
  styleUrls: ['./strategy-lab-validation.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabValidationComponent {
  private marketDataService = inject(MarketDataService);

  // Data source toggle
  dataSource = signal<DataSource>('csv');

  // Form inputs (for Polygon mode)
  ticker = signal('AAPL');
  fromDate = signal('2025-12-15');
  toDate = signal('2026-03-07');
  timeframe = signal('5m');
  filterRth = signal(false);

  // RSI params
  rsiWindow = signal(14);
  oversold = signal(30);
  overbought = signal(70);

  // Date validation
  minDate = getMinAllowedDate();

  readonly timeframeOptions = [
    { label: '1 min', value: '1m' },
    { label: '5 min', value: '5m' },
    { label: '15 min', value: '15m' },
    { label: '30 min', value: '30m' },
    { label: '1 hour', value: '1h' },
  ];

  // State
  loading = signal(false);
  error = signal<string | null>(null);
  result = signal<BacktestResult | null>(null);

  // External trade report (TradingView trade list CSV)
  externalTrades = signal<ExternalTrade[]>([]);
  externalFileName = signal<string | null>(null);

  // Imported OHLCV bars (TradingView bar export CSV)
  importedBars = signal<CsvBar[]>([]);
  barsFileName = signal<string | null>(null);

  // Computed
  winRate = computed(() => {
    const r = this.result();
    if (!r || r.totalTrades === 0) return 0;
    return Math.round((r.winningTrades / r.totalTrades) * 100);
  });

  avgPnl = computed(() => {
    const r = this.result();
    if (!r || r.totalTrades === 0) return 0;
    return r.totalPnL / r.totalTrades;
  });

  externalSummary = computed(() => {
    const trades = this.externalTrades();
    if (!trades.length) return null;
    const totalPnl = trades[trades.length - 1].cumulativePnl;
    const winning = trades.filter(t => t.pnl > 0).length;
    const losing = trades.filter(t => t.pnl <= 0).length;
    const winRate = Math.round((winning / trades.length) * 100);
    return { totalPnl, winning, losing, winRate, totalTrades: trades.length };
  });

  parametersJson = computed(() => JSON.stringify({
    Window: this.rsiWindow(),
    Oversold: this.oversold(),
    Overbought: this.overbought(),
  }));

  private get parsedTimeframe(): { timespan: string; multiplier: number } {
    const tf = this.timeframe();
    if (tf.endsWith('h')) {
      return { timespan: 'hour', multiplier: parseInt(tf) || 1 };
    }
    return { timespan: 'minute', multiplier: parseInt(tf) || 1 };
  }

  // Comparison data
  comparisons = computed<TradeComparison[]>(() => {
    const ext = this.externalTrades();
    const r = this.result();
    if (!ext.length || !r?.trades?.length) return [];

    const ourTrades = r.trades;
    const comparisons: TradeComparison[] = [];
    const maxLen = Math.max(ext.length, ourTrades.length);

    for (let i = 0; i < maxLen; i++) {
      const e = ext[i];
      const o = ourTrades[i];

      comparisons.push({
        tradeNum: i + 1,
        externalType: e?.type ?? '-',
        ourType: o?.tradeType ?? '-',
        externalEntryTime: e?.entryTime ?? '-',
        ourEntryTime: o ? this.formatTimestamp(o.entryTimestamp) : '-',
        externalExitTime: e?.exitTime ?? '-',
        ourExitTime: o ? this.formatTimestamp(o.exitTimestamp) : '-',
        externalEntryPrice: e?.entryPrice ?? 0,
        ourEntryPrice: o?.entryPrice ?? 0,
        externalExitPrice: e?.exitPrice ?? 0,
        ourExitPrice: o?.exitPrice ?? 0,
        externalPnl: e?.pnl ?? 0,
        ourPnl: o?.pnl ?? 0,
        pnlDiff: (o?.pnl ?? 0) - (e?.pnl ?? 0),
        matched: e != null && o != null && Math.abs((o.pnl) - (e.pnl)) < 0.02,
      });
    }

    return comparisons;
  });

  matchRate = computed(() => {
    const c = this.comparisons();
    if (!c.length) return 0;
    return Math.round((c.filter(x => x.matched).length / c.length) * 100);
  });

  // --- File handlers ---

  onTradeReportSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (!input.files?.length) return;

    const file = input.files[0];
    this.externalFileName.set(file.name);

    const reader = new FileReader();
    reader.onload = () => {
      this.parseTradeReportCsv(reader.result as string);
    };
    reader.readAsText(file);
  }

  onBarsCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (!input.files?.length) return;

    const file = input.files[0];
    this.barsFileName.set(file.name);

    const reader = new FileReader();
    reader.onload = () => {
      this.parseBarsCsv(reader.result as string);
    };
    reader.readAsText(file);
  }

  private parseTradeReportCsv(csv: string): void {
    const lines = csv.trim().split('\n');
    if (lines.length < 2) return;

    const trades: ExternalTrade[] = [];
    const rawRows: { tradeNum: number; rowType: string; signal: string; price: number; cumulativePnl: number; pnl: number; dateTime: string }[] = [];

    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(',');
      if (cols.length < 15) continue;

      rawRows.push({
        tradeNum: parseInt(cols[0]),
        rowType: cols[1].trim(),
        dateTime: cols[2].trim(),
        signal: cols[3].trim(),
        price: parseFloat(cols[4]),
        pnl: parseFloat(cols[7]),
        cumulativePnl: parseFloat(cols[14]),
      });
    }

    const grouped = new Map<number, typeof rawRows>();
    for (const row of rawRows) {
      if (!grouped.has(row.tradeNum)) grouped.set(row.tradeNum, []);
      grouped.get(row.tradeNum)!.push(row);
    }

    for (const [tradeNum, rows] of grouped) {
      if (rows.length < 2) continue;

      const entryRow = rows.find(r => r.rowType.startsWith('Entry'));
      const exitRow = rows.find(r => r.rowType.startsWith('Exit'));
      if (!entryRow || !exitRow) continue;

      const type = entryRow.rowType.includes('long') ? 'long' : 'short';

      trades.push({
        tradeNum,
        type,
        entryTime: entryRow.dateTime,
        exitTime: exitRow.dateTime,
        entryPrice: entryRow.price,
        exitPrice: exitRow.price,
        pnl: exitRow.pnl,
        cumulativePnl: exitRow.cumulativePnl,
        signal: entryRow.signal,
      });
    }

    this.externalTrades.set(trades.sort((a, b) => a.tradeNum - b.tradeNum));
  }

  private parseBarsCsv(csv: string): void {
    const lines = csv.trim().split('\n');
    if (lines.length < 2) return;

    const header = lines[0].toLowerCase();
    const cols = header.split(',').map(c => c.trim());

    // Detect column indices — support both our format and TradingView format
    const timeIdx = cols.findIndex(c => c === 'time' || c === 'timestamp' || c === 'date');
    const openIdx = cols.findIndex(c => c === 'open');
    const highIdx = cols.findIndex(c => c === 'high');
    const lowIdx = cols.findIndex(c => c === 'low');
    const closeIdx = cols.findIndex(c => c === 'close');
    const volIdx = cols.findIndex(c => c === 'volume' || c === 'vol');

    if (timeIdx < 0 || openIdx < 0 || highIdx < 0 || lowIdx < 0 || closeIdx < 0) {
      this.error.set('CSV must have columns: time/timestamp, open, high, low, close. Got: ' + header);
      return;
    }

    const bars: CsvBar[] = [];
    for (let i = 1; i < lines.length; i++) {
      const parts = lines[i].split(',');
      if (parts.length < 5) continue;

      const timeStr = parts[timeIdx].trim();
      let timestamp: number;

      // Try parsing as Unix timestamp (seconds or milliseconds)
      const numericTime = Number(timeStr);
      if (!isNaN(numericTime) && numericTime > 1e9) {
        timestamp = numericTime < 1e12 ? numericTime * 1000 : numericTime;
      } else {
        // Try parsing as ISO date string
        const d = new Date(timeStr);
        if (isNaN(d.getTime())) continue;
        timestamp = d.getTime();
      }

      bars.push({
        timestamp,
        open: parseFloat(parts[openIdx]),
        high: parseFloat(parts[highIdx]),
        low: parseFloat(parts[lowIdx]),
        close: parseFloat(parts[closeIdx]),
        volume: volIdx >= 0 ? parseInt(parts[volIdx]) || 0 : 0,
      });
    }

    bars.sort((a, b) => a.timestamp - b.timestamp);
    this.importedBars.set(bars);
  }

  // --- Run backtest ---

  runBacktest(): void {
    if (this.dataSource() === 'csv') {
      this.runFromCsvBars();
    } else {
      this.runFromPolygon();
    }
  }

  private runFromPolygon(): void {
    const dateError = validateDateRange(this.fromDate(), this.toDate());
    if (dateError) {
      this.error.set(dateError);
      return;
    }

    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    const { timespan, multiplier } = this.parsedTimeframe;
    this.marketDataService.runBacktest(
      this.ticker().toUpperCase(),
      'rsi_reversal',
      this.fromDate(),
      this.toDate(),
      timespan,
      multiplier,
      this.parametersJson(),
      this.filterRth(),
    ).subscribe({
      next: (res) => {
        if (!res.success) {
          this.error.set(res.error || 'Backtest failed');
        } else {
          this.result.set(res);
        }
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err.message || 'Request failed');
        this.loading.set(false);
      },
    });
  }

  private runFromCsvBars(): void {
    const bars = this.importedBars();
    if (bars.length < 2) {
      this.error.set('Import TradingView OHLCV bars CSV first (need at least 2 bars)');
      return;
    }

    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    this.marketDataService.runBacktestFromCsvBars(
      'rsi_reversal',
      bars,
      this.parametersJson(),
      this.filterRth(),
    ).subscribe({
      next: (res) => {
        if (!res.success) {
          this.error.set(res.error || 'Backtest failed');
        } else {
          this.result.set(res);
        }
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err.message || 'Request failed');
        this.loading.set(false);
      },
    });
  }

  formatPrice(val: number): string {
    return val.toFixed(2);
  }

  formatTimestamp(iso: string): string {
    if (!iso) return '';
    const hasTimezone = iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso);
    const d = new Date(hasTimezone ? iso : iso + 'Z');
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
      timeZoneName: 'short',
    });
  }
}
