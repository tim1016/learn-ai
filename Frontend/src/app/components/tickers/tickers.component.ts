import { ChangeDetectorRef, Component, inject, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { TickerService } from '../../services/ticker.service';
import { Ticker } from '../../graphql/types';

interface TickerWithStats extends Ticker {
  aggregateCount?: number;
  earliestDate?: string | null;
  latestDate?: string | null;
  statsLoading?: boolean;
}

@Component({
  selector: 'app-tickers',
  standalone: true,
  imports: [CommonModule, RouterLink],
  template: `
    <div class="tickers-page">
      <h1>Tickers</h1>
      <p class="subtitle">Market instruments tracked in your database</p>

      @if (loading) {
        <div class="loading">Loading tickers...</div>
      }

      @if (error) {
        <div class="error">{{ error }}</div>
      }

      @if (!loading && tickers.length === 0 && !error) {
        <div class="empty-state">
          <p>No tickers found. Fetch some market data from the
            <a routerLink="/market-data">Market Data</a> page to get started.
          </p>
        </div>
      }

      @if (tickers.length > 0) {
        <div class="ticker-grid">
          @for (t of tickers; track t.id) {
            <div class="ticker-card">
              <div class="ticker-header">
                <div class="symbol-badge">{{ t.symbol }}</div>
                <span class="status-badge" [class.active]="t.active" [class.inactive]="!t.active">
                  {{ t.active ? 'Active' : 'Inactive' }}
                </span>
              </div>

              <div class="ticker-body">
                <div class="info-row">
                  <span class="label">Market</span>
                  <span class="value">{{ t.market }}</span>
                </div>

                <div class="info-row">
                  <span class="label">Added</span>
                  <span class="value">{{ t.createdAt | date:'mediumDate' }}</span>
                </div>

                @if (t.updatedAt) {
                  <div class="info-row">
                    <span class="label">Last Updated</span>
                    <span class="value">{{ t.updatedAt | date:'medium' }}</span>
                  </div>
                }

                <div class="info-row">
                  <span class="label">Data Points</span>
                  <span class="value">
                    @if (t.statsLoading) {
                      <span class="loading-dot">...</span>
                    } @else {
                      {{ t.aggregateCount ?? '—' }} aggregates
                    }
                  </span>
                </div>

                @if (t.earliestDate && t.latestDate) {
                  <div class="info-row">
                    <span class="label">Date Range</span>
                    <span class="value date-range">
                      {{ t.earliestDate | date:'mediumDate' }} — {{ t.latestDate | date:'mediumDate' }}
                    </span>
                  </div>
                }
              </div>

              @if (t.sanitizationSummary) {
                <div class="sanitization-summary">
                  <span class="summary-label">Sanitization</span>
                  <span class="summary-text">{{ t.sanitizationSummary }}</span>
                </div>
              }
            </div>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    .tickers-page {
      padding: 20px;
      max-width: 1200px;
      margin: 0 auto;
    }

    .subtitle {
      color: #7f8c8d;
      margin-bottom: 24px;
      margin-top: -12px;
    }

    .empty-state {
      text-align: center;
      padding: 60px 20px;
      color: #7f8c8d;
      background: white;
      border-radius: 8px;
      box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }

    .empty-state a {
      color: #3498db;
      text-decoration: none;
      font-weight: 500;
    }

    .ticker-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
      gap: 20px;
    }

    .ticker-card {
      background: white;
      border-radius: 8px;
      box-shadow: 0 2px 4px rgba(0,0,0,0.1);
      overflow: hidden;
    }

    .ticker-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 20px;
      background: #2c3e50;
    }

    .symbol-badge {
      font-size: 20px;
      font-weight: 700;
      color: white;
      letter-spacing: 1px;
    }

    .status-badge {
      font-size: 12px;
      padding: 3px 10px;
      border-radius: 12px;
      font-weight: 500;
    }

    .status-badge.active {
      background: #26a69a;
      color: white;
    }

    .status-badge.inactive {
      background: #ef5350;
      color: white;
    }

    .ticker-body {
      padding: 16px 20px;
    }

    .info-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 0;
      border-bottom: 1px solid #f0f0f0;
    }

    .info-row:last-child {
      border-bottom: none;
    }

    .label {
      color: #7f8c8d;
      font-size: 13px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.3px;
    }

    .value {
      color: #2c3e50;
      font-weight: 500;
      font-size: 14px;
    }

    .date-range {
      font-size: 13px;
    }

    .loading-dot {
      color: #7f8c8d;
    }

    .sanitization-summary {
      padding: 12px 20px;
      background: #f8f9fa;
      border-top: 1px solid #e9ecef;
    }

    .summary-label {
      display: block;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #26a69a;
      margin-bottom: 4px;
    }

    .summary-text {
      font-size: 13px;
      color: #555;
      line-height: 1.5;
    }
  `]
})
export class TickersComponent implements OnInit {
  private tickerService = inject(TickerService);
  private cdr = inject(ChangeDetectorRef);

  tickers: TickerWithStats[] = [];
  loading = false;
  error: string | null = null;

  ngOnInit(): void {
    this.loading = true;
    this.tickerService.getTickers().subscribe({
      next: (tickers) => {
        this.tickers = tickers.map(t => ({ ...t, statsLoading: true }));
        this.loading = false;
        this.cdr.detectChanges();

        // Load aggregate stats for each ticker
        for (const t of this.tickers) {
          this.tickerService.getAggregateStats(t.symbol).subscribe({
            next: (stats) => {
              t.aggregateCount = stats.count;
              t.earliestDate = stats.earliest;
              t.latestDate = stats.latest;
              t.statsLoading = false;
              this.cdr.detectChanges();
            },
            error: () => {
              t.statsLoading = false;
              this.cdr.detectChanges();
            }
          });
        }
      },
      error: (err) => {
        this.error = err.message || 'Failed to load tickers';
        this.loading = false;
        this.cdr.detectChanges();
      }
    });
  }
}
