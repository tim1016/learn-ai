import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { firstValueFrom, forkJoin } from 'rxjs';
import { MarketDataService } from '../../services/market-data.service';
import { TickerInfo, TickerDetailResult, RelatedTickersResult } from '../../graphql/types';

const TRACKED_TICKERS = [
  'NVDA', 'TSLA', 'AAPL', 'AMZN', 'MSFT', 'META', 'GOOGL', 'GOOG', 'AMD', 'PLTR',
  'INTC', 'MU', 'AVGO', 'NFLX', 'WMT', 'JPM', 'BAC', 'XOM', 'V', 'MA',
  'PYPL', 'DIS', 'PFE', 'KO', 'PEP', 'COST', 'LLY', 'NKE', 'CRM', 'ORCL',
  'ADBE', 'CSCO', 'CVX', 'JNJ', 'PG', 'T', 'VZ', 'F', 'GM', 'GE',
  'CAT', 'GS', 'MS', 'ABBV', 'MRK', 'UNH', 'SHOP', 'UBER', 'SQ', 'HOOD',
];

@Component({
  selector: 'app-tracked-instruments',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './tracked-instruments.component.html',
  styleUrls: ['./tracked-instruments.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TrackedInstrumentsComponent implements OnInit {
  private marketDataService = inject(MarketDataService);

  tickerList = signal<TickerInfo[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);

  expandedTicker = signal<string | null>(null);
  detailLoading = signal(false);
  detailCache = signal<Map<string, TickerDetailResult>>(new Map());
  relatedCache = signal<Map<string, RelatedTickersResult>>(new Map());

  get currentDetail(): TickerDetailResult | null {
    const ticker = this.expandedTicker();
    return ticker ? this.detailCache().get(ticker) ?? null : null;
  }

  get currentRelated(): RelatedTickersResult | null {
    const ticker = this.expandedTicker();
    return ticker ? this.relatedCache().get(ticker) ?? null : null;
  }

  async ngOnInit(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const result = await firstValueFrom(
        this.marketDataService.getTrackedTickers(TRACKED_TICKERS)
      );

      if (!result.success) {
        this.error.set(result.error ?? 'Failed to load tickers');
        return;
      }

      // Sort to match the original order
      const orderMap = new Map(TRACKED_TICKERS.map((t, i) => [t, i]));
      const sorted = [...result.tickers].sort(
        (a, b) => (orderMap.get(a.ticker) ?? 999) - (orderMap.get(b.ticker) ?? 999)
      );
      this.tickerList.set(sorted);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.loading.set(false);
    }
  }

  async toggleRow(ticker: string): Promise<void> {
    if (this.expandedTicker() === ticker) {
      this.expandedTicker.set(null);
      return;
    }

    this.expandedTicker.set(ticker);

    // Skip fetch if already cached
    if (this.detailCache().has(ticker)) return;

    this.detailLoading.set(true);

    try {
      const [detail, related] = await firstValueFrom(
        forkJoin([
          this.marketDataService.getTickerDetails(ticker),
          this.marketDataService.getRelatedTickers(ticker),
        ])
      );

      this.detailCache.update(cache => {
        const next = new Map(cache);
        next.set(ticker, detail);
        return next;
      });

      this.relatedCache.update(cache => {
        const next = new Map(cache);
        next.set(ticker, related);
        return next;
      });
    } catch (err) {
      // Show error inline — detail will be null for this ticker
    } finally {
      this.detailLoading.set(false);
    }
  }

  isExpanded(ticker: string): boolean {
    return this.expandedTicker() === ticker;
  }

  formatMarketCap(value: number | null): string {
    if (value == null) return '--';
    if (value >= 1e12) return `$${(value / 1e12).toFixed(2)}T`;
    if (value >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
    if (value >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
    return `$${value.toLocaleString()}`;
  }

  formatEmployees(value: number | null): string {
    if (value == null) return '--';
    if (value >= 1000) return `${(value / 1000).toFixed(1)}K`;
    return value.toLocaleString();
  }

  formatShares(value: number | null): string {
    if (value == null) return '--';
    if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
    if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
    return value.toLocaleString();
  }
}
