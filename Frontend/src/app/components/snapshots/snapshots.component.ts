import { Component, inject, signal, computed, effect, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { MessageService } from 'primeng/api';
import { MarketDataService } from '../../services/market-data.service';
import { gqlResource } from '../../shared/graphql';
import {
  StockSnapshotResult,
  StockTickerSnapshot,
  UnifiedSnapshotItem,
} from '../../graphql/types';

type Tab = 'single' | 'movers' | 'multi' | 'unified';

import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

// Inline GraphQL query — demonstrates the gqlResource pattern. Centralizing
// query strings into graphql/queries.ts is recommended as a follow-up; see
// docs/architecture/frontend-architecture-review-2026-04-23.md.
const GET_STOCK_SNAPSHOT_QUERY = `
  query GetStockSnapshot($ticker: String!) {
    getStockSnapshot(ticker: $ticker) {
      success
      snapshot {
        ticker
        day { open high low close volume vwap }
        prevDay { open high low close volume vwap }
        min { open high low close volume vwap accumulatedVolume timestamp }
        todaysChange todaysChangePercent updated
      }
      error
    }
  }
`;

@Component({
  selector: 'app-snapshots',
  standalone: true,
  imports: [CommonModule, FormsModule, PageHeaderComponent],
  templateUrl: './snapshots.component.html',
  styleUrls: ['./snapshots.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SnapshotsComponent {
  private marketDataService = inject(MarketDataService);
  private messageService = inject(MessageService);

  activeTab = signal<Tab>('single');

  // --- Single Ticker (gqlResource pattern: signal-driven, auto-toast errors) ---
  singleTicker = signal('AAPL');
  private singleSubmittedTicker = signal<string | undefined>(undefined);

  private singleResource = gqlResource<
    { getStockSnapshot: StockSnapshotResult },
    { ticker: string }
  >(
    GET_STOCK_SNAPSHOT_QUERY,
    () => {
      const t = this.singleSubmittedTicker();
      return t ? { ticker: t } : undefined;
    },
    { errorContext: 'Snapshot fetch' },
  );

  singleSnapshot = computed<StockTickerSnapshot | null>(
    () => this.singleResource.value()?.getStockSnapshot.snapshot ?? null,
  );
  singleLoading = this.singleResource.isLoading;

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

  constructor() {
    // Domain-level errors (HTTP 200 with success: false) → toast.
    // Transport / GraphQL errors are toasted by gqlResource itself.
    effect(() => {
      const result = this.singleResource.value()?.getStockSnapshot;
      if (result && !result.success && result.error) {
        this.messageService.add({
          severity: 'warn',
          summary: 'Snapshot unavailable',
          detail: result.error,
          life: 6000,
        });
      }
    });
  }

  fetchSingleSnapshot(): void {
    const ticker = this.singleTicker().trim().toUpperCase();
    if (!ticker) return;
    // Same ticker → force a reload since the params signal hasn't changed.
    if (this.singleSubmittedTicker() === ticker) {
      this.singleResource.reload();
    } else {
      this.singleSubmittedTicker.set(ticker);
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
