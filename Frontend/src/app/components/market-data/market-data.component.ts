import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { PolygonService } from '../../services/polygon.service';
import { GetStocksAggregates200Response } from '@polygon.io/client-js';

/**
 * Example component demonstrating type-safe Polygon.io API usage
 *
 * This component shows how to:
 * 1. Inject the PolygonService
 * 2. Make type-safe API calls
 * 3. Handle loading states and errors
 * 4. Display market data with full TypeScript intellisense
 */
@Component({
  selector: 'app-market-data',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './market-data.component.html',
  styleUrls: ['./market-data.component.scss']
})
export class MarketDataComponent implements OnInit {
  ticker = 'AAPL';
  fromDate = '2024-02-01';
  toDate = '2024-02-08';
  loading = false;
  error: string | null = null;
  results: GetStocksAggregates200Response | null = null;
  snapshotData: any = null;

  constructor(private polygonService: PolygonService) {}

  ngOnInit() {
    // Set default dates
    const today = new Date();
    const lastWeek = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
    this.toDate = today.toISOString().split('T')[0];
    this.fromDate = lastWeek.toISOString().split('T')[0];
  }

  /**
   * Fetch stock aggregates with full type safety
   * TypeScript will provide intellisense for all parameters and response types
   */
  async fetchData() {
    if (!this.ticker) {
      this.error = 'Please enter a ticker symbol';
      return;
    }

    this.loading = true;
    this.error = null;
    this.results = null;
    this.snapshotData = null;

    try {
      // Type-safe API call with full intellisense
      this.results = await this.polygonService.getStockAggregates({
        ticker: this.ticker.toUpperCase(),
        multiplier: 1,
        timespan: 'day',
        from: this.fromDate,
        to: this.toDate,
        adjusted: true,
        sort: 'asc'
      });

      // TypeScript knows the exact structure of results
      console.log('Fetched aggregates:', this.results);
    } catch (err: any) {
      this.error = err?.message || 'Failed to fetch data';
      console.error('Error fetching data:', err);
    } finally {
      this.loading = false;
    }
  }

  /**
   * Example: Get real-time snapshot
   */
  async fetchSnapshot() {
    this.loading = true;
    this.error = null;

    try {
      this.snapshotData = await this.polygonService.getSnapshot('AAPL');
      console.log('Snapshot:', this.snapshotData);
    } catch (err: any) {
      this.error = err?.message || 'Failed to fetch snapshot';
    } finally {
      this.loading = false;
    }
  }

  /**
   * Example: Get last trade
   */
  async fetchLastTrade() {
    this.loading = true;
    this.error = null;

    try {
      this.snapshotData = await this.polygonService.getLastTrade('AAPL');
      console.log('Last trade:', this.snapshotData);
    } catch (err: any) {
      this.error = err?.message || 'Failed to fetch last trade';
    } finally {
      this.loading = false;
    }
  }

  /**
   * Example: Get top gainers
   */
  async fetchGainers() {
    this.loading = true;
    this.error = null;

    try {
      this.snapshotData = await this.polygonService.getGainersLosers('gainers');
      console.log('Gainers:', this.snapshotData);
    } catch (err: any) {
      this.error = err?.message || 'Failed to fetch gainers';
    } finally {
      this.loading = false;
    }
  }

  /**
   * Format Unix timestamp to readable date
   */
  formatDate(timestamp: number): string {
    return new Date(timestamp).toLocaleDateString();
  }
}
