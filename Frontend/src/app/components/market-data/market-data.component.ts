import { Component, OnInit, ChangeDetectorRef, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MarketDataService } from '../../services/market-data.service';
import { StockAggregate, AggregatesSummary } from '../../graphql/types';
import { CandlestickChartComponent } from './candlestick-chart/candlestick-chart.component';
import { LineChartComponent } from './line-chart/line-chart.component';
import { VolumeChartComponent } from './volume-chart/volume-chart.component';
import { SummaryStatsComponent } from './summary-stats/summary-stats.component';
import { TableModule } from 'primeng/table';

@Component({
  selector: 'app-market-data',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    CandlestickChartComponent, LineChartComponent,
    VolumeChartComponent, SummaryStatsComponent,
    TableModule
  ],
  templateUrl: './market-data.component.html',
  styleUrls: ['./market-data.component.scss']
})
export class MarketDataComponent implements OnInit {
  ticker = 'AAPL';
  fromDate = '';
  toDate = '';
  timespan = 'day';
  multiplier = 1;

  loading = false;
  error: string | null = null;
  aggregates: StockAggregate[] = [];
  summary: AggregatesSummary | null = null;

  private cdr = inject(ChangeDetectorRef);

  constructor(private marketDataService: MarketDataService) {}

  ngOnInit(): void {
    const today = new Date();
    const threeMonthsAgo = new Date(today);
    threeMonthsAgo.setMonth(threeMonthsAgo.getMonth() - 3);
    this.toDate = today.toISOString().split('T')[0];
    this.fromDate = threeMonthsAgo.toISOString().split('T')[0];
  }

  fetchData(): void {
    if (!this.ticker) {
      this.error = 'Please enter a ticker symbol';
      return;
    }

    this.loading = true;
    this.error = null;
    this.aggregates = [];
    this.summary = null;

    console.log('[STEP 1 - Component] fetchData called:', {
      ticker: this.ticker.toUpperCase(),
      from: this.fromDate,
      to: this.toDate,
      timespan: this.timespan,
      multiplier: this.multiplier
    });

    this.marketDataService.getOrFetchStockAggregates(
      this.ticker.toUpperCase(),
      this.fromDate,
      this.toDate,
      this.timespan,
      this.multiplier
    ).subscribe({
      next: (result) => {
        console.log('[STEP 2 - Component] GraphQL response received:', {
          ticker: result?.ticker,
          aggregatesCount: result?.aggregates?.length ?? 0,
          hasSummary: !!result?.summary,
          firstBar: result?.aggregates?.[0],
          summary: result?.summary
        });
        this.aggregates = [...result.aggregates].sort(
          (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
        );
        this.summary = result.summary;
        this.loading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        console.error('[STEP 2 - Component] GraphQL ERROR:', {
          message: err?.message,
          networkError: err?.networkError,
          graphQLErrors: err?.graphQLErrors,
          fullError: err
        });
        this.error = err?.message || 'Failed to fetch data';
        this.loading = false;
        this.cdr.detectChanges();
      }
    });
  }
}
