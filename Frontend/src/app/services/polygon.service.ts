import { Injectable } from '@angular/core';
import { restClient, DefaultApi, GetStocksAggregates200Response } from '@polygon.io/client-js';
import { environment } from '../../environments/environment';

/**
 * Type-safe wrapper for Polygon.io REST API client
 *
 * Security Notes:
 * - In production, use backend proxy (environment.useBackendProxy = true)
 * - Never expose API keys in frontend code
 * - For development, set environment.polygonApiKey in environment.development.ts (don't commit)
 */
@Injectable({
  providedIn: 'root'
})
export class PolygonService {
  private client: DefaultApi | null = null;

  constructor() {
    // Only initialize direct client if API key is provided AND not using proxy
    if (environment.polygonApiKey && !environment.useBackendProxy) {
      this.client = restClient(environment.polygonApiKey);
    }
  }

  /**
   * Check if direct API client is available
   */
  private ensureClient(): DefaultApi {
    if (!this.client) {
      throw new Error(
        'Polygon API client not initialized. ' +
        'Either set polygonApiKey in environment or enable backend proxy.'
      );
    }
    return this.client;
  }

  /**
   * Get stock aggregates (OHLCV bars) with full type safety
   *
   * @example
   * const data = await this.polygonService.getStockAggregates({
   *   ticker: 'AAPL',
   *   multiplier: 1,
   *   timespan: 'day',
   *   from: '2024-01-01',
   *   to: '2024-01-31'
   * });
   */
  async getStockAggregates(params: {
    ticker: string;
    multiplier: number;
    timespan: 'minute' | 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year';
    from: string;
    to: string;
    adjusted?: boolean;
    sort?: 'asc' | 'desc';
    limit?: number;
  }): Promise<GetStocksAggregates200Response> {
    const client = this.ensureClient();

    return client.getStocksAggregates(
      params.ticker,
      params.multiplier,
      params.timespan as any,
      params.from,
      params.to,
      params.adjusted ?? true,
      params.sort as any,
      params.limit ?? 50000
    );
  }

  /**
   * Get last trade for a ticker
   */
  async getLastTrade(ticker: string) {
    const client = this.ensureClient();
    return client.getLastStocksTrade(ticker);
  }

  /**
   * Get last quote (bid/ask) for a ticker
   */
  async getLastQuote(ticker: string) {
    const client = this.ensureClient();
    return client.getLastStocksQuote(ticker);
  }

  /**
   * Get snapshot of a ticker (current day aggregate + prev day)
   */
  async getSnapshot(ticker: string) {
    const client = this.ensureClient();
    return client.getStocksSnapshotTicker(ticker);
  }

  /**
   * Get multiple tickers snapshot
   */
  async getTickersSnapshot(tickers?: string[]) {
    const client = this.ensureClient();
    return client.getStocksSnapshotTickers(tickers);
  }

  /**
   * Get gainers/losers snapshot
   */
  async getGainersLosers(direction: 'gainers' | 'losers' = 'gainers') {
    const client = this.ensureClient();
    return client.getStocksSnapshotDirection(direction as any);
  }
}
