import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MarketDataService } from '../../services/market-data.service';
import { StockAggregate, AggregatesSummary } from '../../graphql/types';
import { CandlestickChartComponent } from './candlestick-chart/candlestick-chart.component';
import { LineChartComponent } from './line-chart/line-chart.component';
import { VolumeChartComponent } from './volume-chart/volume-chart.component';
import { SummaryStatsComponent } from './summary-stats/summary-stats.component';

@Component({
  selector: 'app-market-data',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    CandlestickChartComponent, LineChartComponent,
    VolumeChartComponent, SummaryStatsComponent
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

    this.marketDataService.getOrFetchStockAggregates(
      this.ticker.toUpperCase(),
      this.fromDate,
      this.toDate,
      this.timespan,
      this.multiplier
    ).subscribe({
      next: (result) => {
        this.aggregates = result.aggregates;
        this.summary = result.summary;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.message || 'Failed to fetch data';
        this.loading = false;
      }
    });
  }
}
