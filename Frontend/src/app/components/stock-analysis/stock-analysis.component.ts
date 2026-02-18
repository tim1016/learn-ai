import { Component, inject, signal, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../services/market-data.service';
import { StockAggregate } from '../../graphql/types';
import { LineChartComponent } from '../market-data/line-chart/line-chart.component';
import { VolumeChartComponent } from '../market-data/volume-chart/volume-chart.component';
import { ChunkQueueComponent } from './chunk-queue/chunk-queue.component';
import { FetchChunk, ProgressStats, AtmMethod, TradingDay, SelectedContract, ChunkStatus } from './models';
import { selectNearAtmContracts } from './utils';
import { validateDateRange, getMinAllowedDate } from '../../utils/date-validation';

/** Threshold in ms: if a chunk completes faster than this, it was served from cache */
const CACHE_THRESHOLD_MS = 2000;

@Component({
  selector: 'app-stock-analysis',
  standalone: true,
  imports: [CommonModule, FormsModule, LineChartComponent, VolumeChartComponent, ChunkQueueComponent],
  templateUrl: './stock-analysis.component.html',
  styleUrls: ['./stock-analysis.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StockAnalysisComponent {
  private marketDataService = inject(MarketDataService);
  private router = inject(Router);

  // Date validation
  minDate = getMinAllowedDate();

  // Form inputs
  ticker = signal('GLD');
  fromDate = signal(defaultFromDate());
  toDate = signal(defaultToDate());
  chunkDelayMs = signal(12000);

  // Options inputs
  includeOptions = signal(false);
  atmMethod = signal<AtmMethod>('previousClose');

  // Fetch state
  chunks = signal<FetchChunk[]>([]);
  allAggregates = signal<StockAggregate[]>([]);
  isRunning = signal(false);
  abortRequested = signal(false);
  forceRefresh = signal(false);

  // Computed
  sortedAggregates = computed(() =>
    [...this.allAggregates()].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    )
  );

  progressStats = computed<ProgressStats>(() => {
    const ch = this.chunks();
    const aggs = this.allAggregates();
    const completed = ch.filter(c => c.status === 'complete').length;
    const cached = ch.filter(c => c.status === 'cached').length;
    const errors = ch.filter(c => c.status === 'error').length;
    const timestamps = aggs.map(a => a.timestamp).sort();
    return {
      totalChunks: ch.length,
      completedChunks: completed,
      cachedChunks: cached,
      errorChunks: errors,
      totalBars: aggs.length,
      earliestDate: timestamps[0] ?? null,
      latestDate: timestamps[timestamps.length - 1] ?? null,
    };
  });

  progressPercent = computed(() => {
    const stats = this.progressStats();
    return stats.totalChunks > 0
      ? Math.round((stats.completedChunks / stats.totalChunks) * 100)
      : 0;
  });

  canStart = computed(() => !!this.ticker().trim() && !this.isRunning());

  startAnalysis(): void {
    this.forceRefresh.set(false);
    this.beginFetch();
  }

  refreshAnalysis(): void {
    this.forceRefresh.set(true);
    this.beginFetch();
  }

  stopAnalysis(): void {
    this.abortRequested.set(true);
  }

  onChunkView(chunk: FetchChunk): void {
    const t = this.ticker().trim().toUpperCase();
    this.router.navigate(
      ['/stock-analysis/chunk', t, chunk.fromDate, chunk.toDate],
      { queryParams: { atm: this.atmMethod() } }
    );
  }

  async onChunkRefresh(chunk: FetchChunk): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    this.updateChunk(chunk.index, { status: 'fetching', errorMessage: undefined });
    const startMs = performance.now();

    try {
      const result = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(
          t, chunk.fromDate, chunk.toDate, 'minute', 1, true
        )
      );

      const durationMs = Math.round(performance.now() - startMs);
      const barCount = result.aggregates.length;

      this.updateChunk(chunk.index, { status: 'complete', barCount, durationMs });

      // Replace old bars for this chunk's time range, then append new ones
      const from = new Date(chunk.fromDate).getTime();
      const to = new Date(chunk.toDate + 'T23:59:59').getTime();
      this.allAggregates.update(existing =>
        existing.filter(a => {
          const ts = new Date(a.timestamp).getTime();
          return ts < from || ts > to;
        }).concat(result.aggregates)
      );

      // Re-fetch options if enabled
      if (this.includeOptions() && result.aggregates.length > 0) {
        await this.fetchOptionsForChunk(chunk.index, result.aggregates);
      }
    } catch (err) {
      const durationMs = Math.round(performance.now() - startMs);
      const errorMessage = err instanceof Error ? err.message : String(err);
      this.updateChunk(chunk.index, { status: 'error', durationMs, errorMessage });
    }
  }

  private async beginFetch(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    const dateError = validateDateRange(this.fromDate(), this.toDate());
    if (dateError) {
      this.chunks.set([]);
      this.allAggregates.set([]);
      // Surface the error via a single error chunk
      this.chunks.set([{
        index: 0, fromDate: this.fromDate(), toDate: this.toDate(),
        status: 'error', barCount: 0, durationMs: 0, errorMessage: dateError,
      }]);
      return;
    }

    const chunks = generateMonthlyChunks(this.fromDate(), this.toDate());
    this.chunks.set(chunks);
    this.allAggregates.set([]);
    this.isRunning.set(true);
    this.abortRequested.set(false);

    // Check which chunks are already cached in the DB
    try {
      const ranges = chunks.map(c => ({ fromDate: c.fromDate, toDate: c.toDate }));
      const results = await firstValueFrom(
        this.marketDataService.checkCachedRanges(t, ranges, 'minute', 1)
      );
      this.chunks.update(current =>
        current.map((c, i) => results[i]?.isCached ? { ...c, status: 'cached' as const } : c)
      );
    } catch {
      // Cache check failed — proceed with all as pending (no-op)
    }

    this.executeChunks();
  }

  private async executeChunks(): Promise<void> {
    const currentChunks = this.chunks();
    const refresh = this.forceRefresh();

    for (let i = 0; i < currentChunks.length; i++) {
      if (this.abortRequested()) break;

      this.updateChunk(i, { status: 'fetching' });

      const chunk = currentChunks[i];
      const startMs = performance.now();

      try {
        const result = await firstValueFrom(
          this.marketDataService.getOrFetchStockAggregates(
            this.ticker().trim().toUpperCase(),
            chunk.fromDate,
            chunk.toDate,
            'minute',
            1,
            refresh
          )
        );

        const durationMs = Math.round(performance.now() - startMs);
        const barCount = result.aggregates.length;

        this.updateChunk(i, { status: 'complete', barCount, durationMs });
        this.appendAggregates(result.aggregates);

        // Fetch 0DTE options for each trading day in this chunk
        if (this.includeOptions() && result.aggregates.length > 0) {
          await this.fetchOptionsForChunk(i, result.aggregates);
        }
      } catch (err) {
        const durationMs = Math.round(performance.now() - startMs);
        const errorMessage = err instanceof Error ? err.message : String(err);
        this.updateChunk(i, { status: 'error', durationMs, errorMessage });
      }

      // Skip delay for cached chunks (completed fast) or if it's the last chunk
      if (i < currentChunks.length - 1 && !this.abortRequested()) {
        const lastChunk = this.chunks()[i];
        const wasCached = lastChunk.durationMs < CACHE_THRESHOLD_MS;
        if (!wasCached) {
          await delay(this.chunkDelayMs());
        }
      }
    }

    this.isRunning.set(false);
  }

  private async fetchOptionsForChunk(chunkIndex: number, stockBars: StockAggregate[]): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    const refresh = this.forceRefresh();

    // Extract unique trading days from the stock bars
    const dayMap = new Map<string, StockAggregate[]>();
    for (const bar of stockBars) {
      const day = bar.timestamp.split('T')[0];
      if (!dayMap.has(day)) dayMap.set(day, []);
      dayMap.get(day)!.push(bar);
    }

    const sortedDays = [...dayMap.keys()].sort();
    const tradingDays: TradingDay[] = sortedDays.map(date => ({
      date,
      stockBarCount: dayMap.get(date)!.length,
      optionsStatus: 'pending' as ChunkStatus,
      optionsFetchedCount: 0,
      optionsContractCount: 0,
      contracts: [],
    }));

    this.updateChunk(chunkIndex, {
      optionsStatus: 'fetching',
      tradingDays,
      optionsContractCount: 0,
      optionsFetchedCount: 0,
    });

    let totalContracts = 0;
    let totalFetched = 0;

    for (let dayIdx = 0; dayIdx < sortedDays.length; dayIdx++) {
      if (this.abortRequested()) break;

      const dayDate = sortedDays[dayIdx];
      const dayBars = dayMap.get(dayDate)!;

      this.updateTradingDay(chunkIndex, dayIdx, { optionsStatus: 'fetching' });

      try {
        // Calculate ATM price for this day
        const prevDayBars = dayIdx > 0 ? dayMap.get(sortedDays[dayIdx - 1]) : undefined;
        const atmPrice = this.calculateAtmPriceForDay(dayBars, prevDayBars);

        if (atmPrice <= 0) {
          this.updateTradingDay(chunkIndex, dayIdx, { optionsStatus: 'complete' });
          continue;
        }

        // Query contracts expiring on THIS day (0DTE)
        const buffer = Math.max(atmPrice * 0.05, 5);
        const result = await firstValueFrom(
          this.marketDataService.getOptionsContracts(t, {
            asOfDate: dayDate,
            strikePriceGte: Math.floor(atmPrice - buffer),
            strikePriceLte: Math.ceil(atmPrice + buffer),
            expirationDate: dayDate, // 0DTE: expires same day
            limit: 200,
          })
        );

        if (!result.success || result.contracts.length === 0) {
          this.updateTradingDay(chunkIndex, dayIdx, { optionsStatus: 'complete' });
          continue;
        }

        // Select ATM + 2 ITM + 2 OTM for calls and puts
        const selected = selectNearAtmContracts(result.contracts, atmPrice, 2, 2);
        const selectedContracts: SelectedContract[] = selected.map(c => ({
          ticker: c.ticker,
          contractType: c.contractType ?? '',
          strikePrice: c.strikePrice ?? 0,
          expirationDate: c.expirationDate ?? '',
        }));

        totalContracts += selected.length;
        this.updateTradingDay(chunkIndex, dayIdx, {
          optionsContractCount: selected.length,
          contracts: selectedContracts,
        });
        this.updateChunk(chunkIndex, { optionsContractCount: totalContracts });

        // Fetch aggregates for each selected contract
        let dayFetched = 0;
        for (const contract of selected) {
          if (this.abortRequested()) break;

          try {
            await firstValueFrom(
              this.marketDataService.getOrFetchStockAggregates(
                contract.ticker, dayDate, dayDate, 'minute', 1, refresh
              )
            );
            dayFetched++;
            totalFetched++;
            this.updateTradingDay(chunkIndex, dayIdx, { optionsFetchedCount: dayFetched });
            this.updateChunk(chunkIndex, { optionsFetchedCount: totalFetched });
          } catch {
            // Individual contract failure — continue with others
          }

          // Delay between options contract fetches
          if (!this.abortRequested()) {
            await delay(this.chunkDelayMs());
          }
        }

        this.updateTradingDay(chunkIndex, dayIdx, { optionsStatus: 'complete' });
      } catch {
        this.updateTradingDay(chunkIndex, dayIdx, { optionsStatus: 'error' });
      }
    }

    this.updateChunk(chunkIndex, {
      optionsStatus: this.abortRequested() ? 'error' : 'complete',
    });
  }

  private calculateAtmPriceForDay(
    currentDayBars: StockAggregate[],
    previousDayBars: StockAggregate[] | undefined
  ): number {
    if (currentDayBars.length === 0) return 0;

    const sortBars = (bars: StockAggregate[]) =>
      [...bars].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

    if (this.atmMethod() === 'currentOpen') {
      return sortBars(currentDayBars)[0].open;
    }

    // previousClose: use the last bar's close of the previous day
    if (previousDayBars && previousDayBars.length > 0) {
      const sorted = sortBars(previousDayBars);
      return sorted[sorted.length - 1].close;
    }

    // Fallback for first day in chunk: check accumulated data
    const currentDate = currentDayBars[0].timestamp.split('T')[0];
    const prevDayBars = this.allAggregates()
      .filter(b => b.timestamp.split('T')[0] < currentDate)
      .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

    if (prevDayBars.length > 0) {
      return prevDayBars[0].close;
    }

    // Last resort: use current day's first open
    return sortBars(currentDayBars)[0].open;
  }

  private updateChunk(index: number, partial: Partial<FetchChunk>): void {
    this.chunks.update(chunks =>
      chunks.map((c, i) => (i === index ? { ...c, ...partial } : c))
    );
  }

  private updateTradingDay(chunkIndex: number, dayIndex: number, partial: Partial<TradingDay>): void {
    this.chunks.update(chunks =>
      chunks.map((c, i) => {
        if (i !== chunkIndex || !c.tradingDays) return c;
        const updatedDays = c.tradingDays.map((d, j) =>
          j === dayIndex ? { ...d, ...partial } : d
        );
        return { ...c, tradingDays: updatedDays };
      })
    );
  }

  private appendAggregates(newBars: StockAggregate[]): void {
    this.allAggregates.update(existing => {
      const existingTimestamps = new Set(existing.map(a => a.timestamp));
      const unique = newBars.filter(b => !existingTimestamps.has(b.timestamp));
      return [...existing, ...unique];
    });
  }

}

function defaultFromDate(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 2);
  return d.toISOString().split('T')[0];
}

function defaultToDate(): string {
  return new Date().toISOString().split('T')[0];
}

export function generateMonthlyChunks(fromDate: string, toDate: string): FetchChunk[] {
  const chunks: FetchChunk[] = [];
  const start = new Date(fromDate + 'T00:00:00');
  const end = new Date(toDate + 'T00:00:00');
  let cursor = new Date(start);
  let index = 0;

  while (cursor < end) {
    const chunkStart = new Date(cursor);

    // Move cursor to the 1st of the next month
    cursor = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);

    // Chunk end is the day before next month's 1st, or the overall end date
    const lastDayOfMonth = new Date(cursor.getTime() - 86400000);
    const chunkEnd = lastDayOfMonth > end ? end : lastDayOfMonth;

    chunks.push({
      index: index++,
      fromDate: formatDate(chunkStart),
      toDate: formatDate(chunkEnd),
      status: 'pending',
      barCount: 0,
      durationMs: 0,
    });
  }

  return chunks;
}

function formatDate(d: Date): string {
  return d.toISOString().split('T')[0];
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}
