import { Component, inject, signal, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../services/market-data.service';
import {
  StockTickerSnapshot,
  UnifiedSnapshotItem,
} from '../../graphql/types';

type Tab = 'single' | 'movers' | 'multi' | 'unified';

@Component({
  selector: 'app-snapshots',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './snapshots.component.html',
  styleUrls: ['./snapshots.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SnapshotsComponent {
  private marketDataService = inject(MarketDataService);

  activeTab = signal<Tab>('single');

  // --- Single Ticker ---
  singleTicker = signal('AAPL');
  singleSnapshot = signal<StockTickerSnapshot | null>(null);
  singleLoading = signal(false);
  singleError = signal<string | null>(null);

  // --- Market Movers ---
  moversDirection = signal<'gainers' | 'losers'>('gainers');
  moversData = signal<StockTickerSnapshot[]>([]);
  moversLoading = signal(false);
  moversError = signal<string | null>(null);

  // --- Multi-Ticker ---
  multiTickersInput = signal('AAPL, MSFT, GOOGL, AMZN, TSLA');
  multiSnapshots = signal<StockTickerSnapshot[]>([]);
  multiLoading = signal(false);
  multiError = signal<string | null>(null);

  // --- Unified ---
  unifiedTickersInput = signal('AAPL, MSFT, GOOGL');
  unifiedLimit = signal(10);
  unifiedResults = signal<UnifiedSnapshotItem[]>([]);
  unifiedLoading = signal(false);
  unifiedError = signal<string | null>(null);

  get parsedMultiTickers(): string[] {
    return this.multiTickersInput()
      .split(',')
      .map(t => t.trim().toUpperCase())
      .filter(t => t.length > 0);
  }

  get parsedUnifiedTickers(): string[] | undefined {
    const input = this.unifiedTickersInput().trim();
    if (!input) return undefined;
    return input
      .split(',')
      .map(t => t.trim().toUpperCase())
      .filter(t => t.length > 0);
  }

  // --- Computed sort helpers ---
  sortedMovers = computed(() =>
    [...this.moversData()].sort((a, b) =>
      Math.abs(b.todaysChangePercent ?? 0) - Math.abs(a.todaysChangePercent ?? 0)
    )
  );

  async fetchSingleSnapshot(): Promise<void> {
    const ticker = this.singleTicker().trim().toUpperCase();
    if (!ticker) return;

    this.singleLoading.set(true);
    this.singleError.set(null);
    this.singleSnapshot.set(null);

    try {
      const result = await firstValueFrom(this.marketDataService.getStockSnapshot(ticker));
      if (!result.success) {
        this.singleError.set(result.error ?? 'Failed to fetch snapshot');
        return;
      }
      this.singleSnapshot.set(result.snapshot);
    } catch (err) {
      this.singleError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.singleLoading.set(false);
    }
  }

  async fetchMovers(): Promise<void> {
    this.moversLoading.set(true);
    this.moversError.set(null);
    this.moversData.set([]);

    try {
      const result = await firstValueFrom(
        this.marketDataService.getMarketMovers(this.moversDirection())
      );
      if (!result.success) {
        this.moversError.set(result.error ?? 'Failed to fetch movers');
        return;
      }
      this.moversData.set(result.tickers);
    } catch (err) {
      this.moversError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.moversLoading.set(false);
    }
  }

  async fetchMultiSnapshots(): Promise<void> {
    const tickers = this.parsedMultiTickers;
    if (tickers.length === 0) return;

    this.multiLoading.set(true);
    this.multiError.set(null);
    this.multiSnapshots.set([]);

    try {
      const result = await firstValueFrom(this.marketDataService.getStockSnapshots(tickers));
      if (!result.success) {
        this.multiError.set(result.error ?? 'Failed to fetch snapshots');
        return;
      }
      this.multiSnapshots.set(result.snapshots);
    } catch (err) {
      this.multiError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.multiLoading.set(false);
    }
  }

  async fetchUnified(): Promise<void> {
    this.unifiedLoading.set(true);
    this.unifiedError.set(null);
    this.unifiedResults.set([]);

    try {
      const result = await firstValueFrom(
        this.marketDataService.getUnifiedSnapshot(this.parsedUnifiedTickers, this.unifiedLimit())
      );
      if (!result.success) {
        this.unifiedError.set(result.error ?? 'Failed to fetch unified snapshots');
        return;
      }
      this.unifiedResults.set(result.results);
    } catch (err) {
      this.unifiedError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.unifiedLoading.set(false);
    }
  }

  formatPrice(val: number | null): string {
    return val != null ? val.toFixed(2) : '--';
  }

  formatChange(val: number | null): string {
    if (val == null) return '--';
    const sign = val >= 0 ? '+' : '';
    return `${sign}${val.toFixed(2)}`;
  }

  formatPercent(val: number | null): string {
    if (val == null) return '--';
    const sign = val >= 0 ? '+' : '';
    return `${sign}${val.toFixed(2)}%`;
  }

  formatVolume(val: number | null): string {
    if (val == null) return '--';
    if (val >= 1_000_000) return (val / 1_000_000).toFixed(1) + 'M';
    if (val >= 1_000) return (val / 1_000).toFixed(1) + 'K';
    return val.toLocaleString();
  }

  changeClass(val: number | null): string {
    if (val == null) return '';
    return val >= 0 ? 'positive' : 'negative';
  }
}
