import { Component, inject, signal, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { firstValueFrom, forkJoin, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { MarketDataService } from '../../services/market-data.service';
import { StockAggregate } from '../../graphql/types';
import { validateDateRange, getMinAllowedDate } from '../../utils/date-validation';
import { LineChartComponent } from '../market-data/line-chart/line-chart.component';
import { VolumeChartComponent } from '../market-data/volume-chart/volume-chart.component';

export interface ContractRow {
  optionTicker: string;
  contractType: 'call' | 'put';
  strikePrice: number;
  dailyBar: StockAggregate | null;
  prevDayClose: number | null;
  changeFromPrevClose: number | null;
  changePercent: number | null;
  isAtm: boolean;
  relativeStrike: number; // e.g., 0 = ATM, +1, -2
}

export interface ScanResult {
  strikePrice: number;
  callTicker: string;
  callHasData: boolean;
  putTicker: string;
  putHasData: boolean;
  selected: boolean; // whether this strike made it into the final chain
}

@Component({
  selector: 'app-options-history',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, LineChartComponent, VolumeChartComponent],
  templateUrl: './options-history.component.html',
  styleUrls: ['./options-history.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsHistoryComponent {
  private marketDataService = inject(MarketDataService);

  minDate = getMinAllowedDate();

  ticker = signal('AAPL');
  analysisDate = signal(OptionsHistoryComponent.getLastWeekday());
  atmMethod = signal<'open' | 'prevClose'>('open');
  numStrikes = signal(5);
  loading = signal(false);
  loadingMessage = signal('');
  error = signal<string | null>(null);

  // Results
  atmPrice = signal<number | null>(null);
  atmStrikeValue = signal<number | null>(null);
  prevDayClosePrice = signal<number | null>(null);
  openPrice = signal<number | null>(null);
  contractRows = signal<ContractRow[]>([]);

  // Stock minute data (fetched once per analyze)
  stockMinuteBars = signal<StockAggregate[]>([]);

  // Scan results (all attempted strikes and whether data was found)
  scanResults = signal<ScanResult[]>([]);

  // Detail panel
  expandedContract = signal<string | null>(null);
  detailBars = signal<StockAggregate[]>([]);
  detailLoading = signal(false);

  // Computed: split rows into calls and puts sorted by strike
  callRows = computed(() =>
    this.contractRows()
      .filter(r => r.contractType === 'call')
      .sort((a, b) => a.strikePrice - b.strikePrice)
  );

  putRows = computed(() =>
    this.contractRows()
      .filter(r => r.contractType === 'put')
      .sort((a, b) => a.strikePrice - b.strikePrice)
  );

  // Unique strikes across both calls and puts
  strikes = computed(() => {
    const s = new Set<number>();
    for (const r of this.contractRows()) {
      s.add(r.strikePrice);
    }
    return [...s].sort((a, b) => a - b);
  });

  // Maps for quick lookup
  callByStrike = computed(() => {
    const map = new Map<number, ContractRow>();
    for (const r of this.callRows()) {
      map.set(r.strikePrice, r);
    }
    return map;
  });

  putByStrike = computed(() => {
    const map = new Map<number, ContractRow>();
    for (const r of this.putRows()) {
      map.set(r.strikePrice, r);
    }
    return map;
  });

  atmStrike = computed(() => this.atmStrikeValue());

  selectedScanCount = computed(() => this.scanResults().filter(r => r.selected).length);

  async analyze(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) { this.error.set('Enter a ticker symbol.'); return; }

    const date = this.analysisDate();
    const dateError = validateDateRange(date, date);
    if (dateError) { this.error.set(dateError); return; }

    this.loading.set(true);
    this.error.set(null);
    this.contractRows.set([]);
    this.scanResults.set([]);
    this.expandedContract.set(null);
    this.detailBars.set([]);
    this.stockMinuteBars.set([]);
    this.atmPrice.set(null);
    this.atmStrikeValue.set(null);
    this.openPrice.set(null);
    this.prevDayClosePrice.set(null);

    try {
      // Step 1: Fetch stock daily bar for analysis date
      this.loadingMessage.set('Fetching stock data...');
      const stockResult = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(t, date, date, 'day', 1)
      );

      const dayBar = stockResult.aggregates?.[0];
      const open = dayBar?.open ?? null;
      this.openPrice.set(open);

      // Step 1b: Fetch stock minute data for the analysis day (for detail charts)
      this.loadingMessage.set('Fetching stock minute data...');
      try {
        const stockMinuteResult = await firstValueFrom(
          this.marketDataService.getOrFetchStockAggregates(t, date, date, 'minute', 1)
        );
        this.stockMinuteBars.set(stockMinuteResult.aggregates ?? []);
      } catch {
        this.stockMinuteBars.set([]);
      }

      // Step 2: Fetch previous trading day's close (look back 7 calendar days)
      this.loadingMessage.set('Fetching previous day close...');
      const prevFrom = this.subtractDays(date, 7);
      const prevTo = this.subtractDays(date, 1);
      const prevResult = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(t, prevFrom, prevTo, 'day', 1)
      );

      const prevBars = prevResult.aggregates ?? [];
      const prevClose = prevBars.length > 0 ? prevBars[prevBars.length - 1].close : null;
      this.prevDayClosePrice.set(prevClose);

      // Step 3: Determine ATM price and round to nearest integer strike
      const rawAtm = this.atmMethod() === 'open' ? open : prevClose;
      if (rawAtm == null) {
        this.error.set(`Could not determine ATM price. No ${this.atmMethod() === 'open' ? 'opening' : 'previous close'} data found for ${t} on ${date}.`);
        this.loading.set(false);
        return;
      }
      this.atmPrice.set(rawAtm);
      const atmStrike = Math.round(rawAtm);
      this.atmStrikeValue.set(atmStrike);

      // Step 4: Search a wider range to find N strikes with actual data per side
      const n = this.numStrikes();
      const searchRange = n * 5; // Scan 5x wider to find enough strikes with data
      const strikeOffsets: number[] = [];
      for (let i = -searchRange; i <= searchRange; i++) {
        strikeOffsets.push(i);
      }

      // Step 5: Construct option tickers using OCC format
      // Format: O:{TICKER}{YYMMDD}{C|P}{STRIKE*1000 zero-padded to 8}
      const expDateObj = new Date(date + 'T00:00:00');
      const yy = String(expDateObj.getFullYear()).slice(-2);
      const mm = String(expDateObj.getMonth() + 1).padStart(2, '0');
      const dd = String(expDateObj.getDate()).padStart(2, '0');
      const formattedExp = `${yy}${mm}${dd}`;

      interface TickerEntry {
        optionTicker: string;
        contractType: 'call' | 'put';
        strikePrice: number;
        offset: number;
      }

      const tickerEntries: TickerEntry[] = [];
      for (const offset of strikeOffsets) {
        const strike = atmStrike + offset;
        if (strike <= 0) continue;
        const formattedStrike = String(strike * 1000).padStart(8, '0');
        const callTicker = `O:${t}${formattedExp}C${formattedStrike}`;
        const putTicker = `O:${t}${formattedExp}P${formattedStrike}`;
        tickerEntries.push({ optionTicker: callTicker, contractType: 'call', strikePrice: strike, offset });
        tickerEntries.push({ optionTicker: putTicker, contractType: 'put', strikePrice: strike, offset });
      }

      // Step 6: Fetch OHLC for all candidates (batched to avoid overwhelming backend)
      const prevDay = prevBars.length > 0
        ? new Date(prevBars[prevBars.length - 1].timestamp).toISOString().slice(0, 10)
        : prevTo;

      const batchSize = 30;
      const ohlcResults: Array<{ aggregates?: StockAggregate[] | null }> = [];
      for (let bi = 0; bi < tickerEntries.length; bi += batchSize) {
        const batchEntries = tickerEntries.slice(bi, bi + batchSize);
        const progress = Math.min(bi + batchSize, tickerEntries.length);
        this.loadingMessage.set(`Scanning contracts (${progress}/${tickerEntries.length})...`);
        const batchObservables = batchEntries.map(entry =>
          this.marketDataService.getOrFetchStockAggregates(
            entry.optionTicker, prevDay, date, 'day', 1
          ).pipe(catchError(() => of({ ticker: entry.optionTicker, aggregates: [] as StockAggregate[], summary: null })))
        );
        const batchResults = await firstValueFrom(forkJoin(batchObservables));
        ohlcResults.push(...batchResults);
      }

      // Step 7: Build all ContractRow[]
      const allRows: ContractRow[] = tickerEntries.map((entry, i) => {
        const result = ohlcResults[i];
        const bars = result.aggregates ?? [];

        let analysisDayBar: StockAggregate | null = null;
        let prevDayBar: StockAggregate | null = null;

        for (const bar of bars) {
          const barDate = bar.timestamp.slice(0, 10);
          if (barDate === date) {
            analysisDayBar = bar;
          } else if (barDate < date) {
            prevDayBar = bar;
          }
        }

        if (!analysisDayBar && bars.length > 0) {
          analysisDayBar = bars[bars.length - 1];
          if (bars.length > 1) {
            prevDayBar = bars[bars.length - 2];
          }
        }

        const pdc = prevDayBar?.close ?? null;
        const dayClose = analysisDayBar?.close ?? null;
        const change = (dayClose != null && pdc != null) ? dayClose - pdc : null;
        const changePct = (change != null && pdc != null && pdc !== 0) ? (change / pdc) * 100 : null;

        return {
          optionTicker: entry.optionTicker,
          contractType: entry.contractType,
          strikePrice: entry.strikePrice,
          dailyBar: analysisDayBar,
          prevDayClose: pdc,
          changeFromPrevClose: change,
          changePercent: changePct,
          isAtm: entry.offset === 0,
          relativeStrike: entry.offset,
        };
      });

      // Step 8: Filter to only strikes with data, take N closest per direction
      // A strike "has data" if either its call or put has a non-null dailyBar
      const strikeHasData = new Set<number>();
      for (const row of allRows) {
        if (row.dailyBar != null) {
          strikeHasData.add(row.strikePrice);
        }
      }

      const strikesWithData = [...strikeHasData].sort((a, b) => a - b);
      const strikesAbove = strikesWithData.filter(s => s > atmStrike).slice(0, n);
      const strikesBelow = strikesWithData.filter(s => s < atmStrike).slice(-n);
      const atmInSet = strikeHasData.has(atmStrike) ? [atmStrike] : [];

      const selectedStrikes = new Set([...strikesBelow, ...atmInSet, ...strikesAbove]);

      // Build scan results: group allRows by strike
      const scanMap = new Map<number, { call?: ContractRow; put?: ContractRow }>();
      for (const row of allRows) {
        if (!scanMap.has(row.strikePrice)) scanMap.set(row.strikePrice, {});
        const entry = scanMap.get(row.strikePrice)!;
        if (row.contractType === 'call') entry.call = row;
        else entry.put = row;
      }
      const scanResultsList: ScanResult[] = [...scanMap.entries()]
        .sort(([a], [b]) => a - b)
        .filter(([, v]) => v.call?.dailyBar != null || v.put?.dailyBar != null) // only show strikes where at least one side had data
        .map(([strike, v]) => ({
          strikePrice: strike,
          callTicker: v.call?.optionTicker ?? '',
          callHasData: v.call?.dailyBar != null,
          putTicker: v.put?.optionTicker ?? '',
          putHasData: v.put?.dailyBar != null,
          selected: selectedStrikes.has(strike),
        }));
      this.scanResults.set(scanResultsList);

      // Keep both call and put rows for selected strikes
      const filteredRows = allRows.filter(row => selectedStrikes.has(row.strikePrice));

      this.contractRows.set(filteredRows);
      this.loadingMessage.set('');
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.loading.set(false);
    }
  }

  async loadMinuteDetail(optionsTicker: string): Promise<void> {
    if (this.expandedContract() === optionsTicker) {
      this.expandedContract.set(null);
      this.detailBars.set([]);
      return;
    }

    this.expandedContract.set(optionsTicker);
    this.detailLoading.set(true);
    this.detailBars.set([]);

    try {
      const date = this.analysisDate();
      const result = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(optionsTicker, date, date, 'minute', 1)
      );
      this.detailBars.set(result.aggregates ?? []);
    } catch (err) {
      console.error('Failed to load minute detail:', err);
    } finally {
      this.detailLoading.set(false);
    }
  }

  handleRowClick(strike: number, side: 'call' | 'put'): void {
    const row = side === 'call'
      ? this.callByStrike().get(strike)
      : this.putByStrike().get(strike);
    if (row) this.loadMinuteDetail(row.optionTicker);
  }

  getMarketDataLink(optionTicker: string): Record<string, string> {
    const date = this.analysisDate();
    return {
      ticker: optionTicker,
      fromDate: this.subtractDays(date, 5),
      toDate: date,
      timespan: 'minute',
    };
  }

  isAtm(strike: number): boolean {
    return strike === this.atmStrike();
  }

  formatPrice(val: number | null | undefined): string {
    return val != null ? val.toFixed(2) : '--';
  }

  formatChange(val: number | null): string {
    if (val == null) return '--';
    const sign = val >= 0 ? '+' : '';
    return `${sign}${val.toFixed(2)}`;
  }

  formatChangePct(val: number | null): string {
    if (val == null) return '--';
    const sign = val >= 0 ? '+' : '';
    return `${sign}${val.toFixed(1)}%`;
  }

  formatVolume(val: number | null | undefined): string {
    if (val == null) return '--';
    return val.toLocaleString();
  }

  private subtractDays(dateStr: string, days: number): string {
    const d = new Date(dateStr + 'T00:00:00');
    d.setDate(d.getDate() - days);
    return d.toISOString().slice(0, 10);
  }

  private static getLastWeekday(): string {
    const d = new Date();
    d.setDate(d.getDate() - 1); // start from yesterday
    while (d.getDay() === 0 || d.getDay() === 6) {
      d.setDate(d.getDate() - 1);
    }
    return d.toISOString().slice(0, 10);
  }
}
