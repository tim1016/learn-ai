import { Injectable, inject, signal, computed } from '@angular/core';
import { StockAggregate } from '../graphql/types';
import { MarketDataService } from './market-data.service';

interface CacheKey {
  ticker: string;
  fromDate: string;
  toDate: string;
  timespan: string;
  multiplier: number;
}

interface CacheEntry {
  aggregates: StockAggregate[];
  timestamp: number;
}

/**
 * Shared store for stock aggregates with normalized caching.
 * Prevents duplicate API calls and provides single source of truth.
 *
 * Features:
 * - Automatic cache invalidation (1 hour TTL)
 * - Prevents N+1 requests for same date range
 * - Merges overlapping date ranges
 * - Signal-based reactivity
 */
@Injectable({
  providedIn: 'root'
})
export class StockAggregateStore {
  private marketDataService = inject(MarketDataService);

  // Internal cache with TTL
  private cache = signal<Map<string, CacheEntry>>(new Map());
  private readonly CACHE_TTL_MS = 60 * 60 * 1000; // 1 hour

  // Public API: get aggregates by ticker
  getAggregatesByTicker = computed(() => {
    const cacheMap = this.cache();
    const result = new Map<string, StockAggregate[]>();

    const now = Date.now();
    for (const [key, entry] of cacheMap.entries()) {
      // Skip if expired
      if (now - entry.timestamp > this.CACHE_TTL_MS) {
        continue;
      }

      // Extract ticker from cache key
      const cachedTicker = key.split('|')[0];
      if (!result.has(cachedTicker)) {
        result.set(cachedTicker, []);
      }

      // Merge aggregates
      const existing = result.get(cachedTicker)!;
      const merged = [...existing, ...entry.aggregates];

      // Deduplicate by timestamp
      const uniqueMap = new Map<string, StockAggregate>();
      for (const agg of merged) {
        uniqueMap.set(agg.timestamp, agg);
      }

      result.set(cachedTicker, Array.from(uniqueMap.values()));
    }

    return result;
  });

  /**
   * Get or fetch aggregates with caching
   * If data is cached and fresh, returns immediately
   * Otherwise fetches from service and caches result
   */
  getOrFetch(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan: string = 'day',
    multiplier: number = 1,
    forceRefresh: boolean = false
  ): Promise<{ aggregates: StockAggregate[] }> {
    return new Promise(async (resolve, reject) => {
      try {
        // Check cache first if not forcing refresh
        if (!forceRefresh) {
          const cached = this.getCached(ticker, fromDate, toDate, timespan, multiplier);
          if (cached) {
            return resolve({ aggregates: cached });
          }
        }

        // Fetch from service
        const result = await this.marketDataService.getOrFetchStockAggregates(
          ticker,
          fromDate,
          toDate,
          timespan,
          multiplier,
          forceRefresh
        ).toPromise();

        if (!result) {
          return reject(new Error('Empty response from market data service'));
        }

        // Cache the result
        this.setCached(
          ticker,
          fromDate,
          toDate,
          timespan,
          multiplier,
          result.aggregates
        );

        resolve(result);
      } catch (error) {
        reject(error);
      }
    });
  }

  /**
   * Clear cache for a specific ticker
   */
  invalidateTicker(ticker: string): void {
    this.cache.update(cacheMap => {
      const newMap = new Map(cacheMap);
      for (const [key] of newMap.entries()) {
        if (key.startsWith(ticker + '|')) {
          newMap.delete(key);
        }
      }
      return newMap;
    });
  }

  /**
   * Clear entire cache
   */
  clearCache(): void {
    this.cache.set(new Map());
  }

  /**
   * Get cache statistics (for debugging)
   */
  getCacheStats(): { size: number; entries: number } {
    const cacheMap = this.cache();
    return {
      size: cacheMap.size,
      entries: Array.from(cacheMap.values()).reduce((sum, entry) => sum + entry.aggregates.length, 0)
    };
  }

  private getCacheKey(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan: string,
    multiplier: number
  ): string {
    return `${ticker}|${fromDate}|${toDate}|${timespan}|${multiplier}`;
  }

  private getCached(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan: string,
    multiplier: number
  ): StockAggregate[] | null {
    const key = this.getCacheKey(ticker, fromDate, toDate, timespan, multiplier);
    const entry = this.cache().get(key);

    if (!entry) {
      return null;
    }

    // Check if expired
    if (Date.now() - entry.timestamp > this.CACHE_TTL_MS) {
      // Evict expired entry
      this.cache.update(cacheMap => {
        const newMap = new Map(cacheMap);
        newMap.delete(key);
        return newMap;
      });
      return null;
    }

    return entry.aggregates;
  }

  private setCached(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan: string,
    multiplier: number,
    aggregates: StockAggregate[]
  ): void {
    const key = this.getCacheKey(ticker, fromDate, toDate, timespan, multiplier);

    this.cache.update(cacheMap => {
      const newMap = new Map(cacheMap);
      newMap.set(key, {
        aggregates: [...aggregates], // Shallow copy to prevent external mutations
        timestamp: Date.now()
      });
      return newMap;
    });
  }
}
