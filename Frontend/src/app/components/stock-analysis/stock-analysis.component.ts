import { Component, inject, signal, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../services/market-data.service';
import { StockAggregate } from '../../graphql/types';
import { CandlestickChartComponent } from '../market-data/candlestick-chart/candlestick-chart.component';
import { VolumeChartComponent } from '../market-data/volume-chart/volume-chart.component';
import { ChunkQueueComponent } from './chunk-queue/chunk-queue.component';
import { FetchChunk, ProgressStats } from './models';

/** Threshold in ms: if a chunk completes faster than this, it was served from cache */
const CACHE_THRESHOLD_MS = 2000;

@Component({
  selector: 'app-stock-analysis',
  standalone: true,
  imports: [CommonModule, FormsModule, CandlestickChartComponent, VolumeChartComponent, ChunkQueueComponent],
  templateUrl: './stock-analysis.component.html',
  styleUrls: ['./stock-analysis.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StockAnalysisComponent {
  private marketDataService = inject(MarketDataService);

  // Form inputs
  ticker = signal('GLD');
  fromDate = signal(defaultFromDate());
  toDate = signal(defaultToDate());
  chunkDelayMs = signal(12000);

  // Fetch state
  chunks = signal<FetchChunk[]>([]);
  allAggregates = signal<StockAggregate[]>([]);
  chunkAggregates = signal<Map<number, StockAggregate[]>>(new Map());
  isRunning = signal(false);
  abortRequested = signal(false);
  forceRefresh = signal(false);

  // Chunk selection state
  selectedChunk = signal<FetchChunk | null>(null);

  // Computed
  sortedAggregates = computed(() =>
    [...this.allAggregates()].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    )
  );

  displayedAggregates = computed<StockAggregate[]>(() => {
    const chunk = this.selectedChunk();
    if (chunk) {
      const chunkBars = this.chunkAggregates().get(chunk.index) ?? [];
      return [...chunkBars].sort(
        (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      );
    }
    return this.sortedAggregates();
  });

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

  onChunkSelected(chunk: FetchChunk): void {
    // Toggle: clicking the same chunk deselects it
    if (this.selectedChunk()?.index === chunk.index) {
      this.selectedChunk.set(null);
    } else {
      this.selectedChunk.set(chunk);
    }
  }

  clearSelection(): void {
    this.selectedChunk.set(null);
  }

  private async beginFetch(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    const chunks = generateMonthlyChunks(this.fromDate(), this.toDate());
    this.chunks.set(chunks);
    this.allAggregates.set([]);
    this.chunkAggregates.set(new Map());
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
        this.storeChunkAggregates(i, result.aggregates);
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

  private updateChunk(index: number, partial: Partial<FetchChunk>): void {
    this.chunks.update(chunks =>
      chunks.map((c, i) => (i === index ? { ...c, ...partial } : c))
    );
  }

  private appendAggregates(newBars: StockAggregate[]): void {
    this.allAggregates.update(existing => {
      const existingTimestamps = new Set(existing.map(a => a.timestamp));
      const unique = newBars.filter(b => !existingTimestamps.has(b.timestamp));
      return [...existing, ...unique];
    });
  }

  private storeChunkAggregates(index: number, bars: StockAggregate[]): void {
    this.chunkAggregates.update(map => {
      const updated = new Map(map);
      updated.set(index, bars);
      return updated;
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
