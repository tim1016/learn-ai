import { Component, inject, ChangeDetectorRef, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MarketDataService } from '../../../services/market-data.service';
import { StockAggregate, IndicatorSeries } from '../../../graphql/types';
import { TaChartComponent } from '../ta-chart/ta-chart.component';

@Component({
  selector: 'app-technical-analysis',
  standalone: true,
  imports: [CommonModule, FormsModule, TaChartComponent],
  templateUrl: './technical-analysis.component.html',
  styleUrls: ['./technical-analysis.component.scss']
})
export class TechnicalAnalysisComponent implements OnInit {
  private cdr = inject(ChangeDetectorRef);
  private marketDataService = inject(MarketDataService);

  ticker = 'AAPL';
  fromDate = '';
  toDate = '';
  timespan = 'day';
  multiplier = 1;

  showSma = true;
  smaWindow = 20;
  showEma = true;
  emaWindow = 50;
  showRsi = true;
  rsiWindow = 14;

  loading = false;
  error: string | null = null;
  message: string | null = null;
  aggregates: StockAggregate[] = [];
  indicators: IndicatorSeries[] = [];

  ngOnInit(): void {
    const today = new Date();
    const sixMonthsAgo = new Date(today);
    sixMonthsAgo.setMonth(sixMonthsAgo.getMonth() - 6);
    this.toDate = today.toISOString().split('T')[0];
    this.fromDate = sixMonthsAgo.toISOString().split('T')[0];
  }

  fetchAndCalculate(): void {
    if (!this.ticker) {
      this.error = 'Please enter a ticker symbol';
      return;
    }

    this.loading = true;
    this.error = null;
    this.message = null;

    this.marketDataService.getOrFetchStockAggregates(
      this.ticker.toUpperCase(), this.fromDate, this.toDate,
      this.timespan, this.multiplier
    ).subscribe({
      next: (result) => {
        this.aggregates = [...result.aggregates].sort(
          (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
        );
        this.calculateIndicators();
      },
      error: (err) => {
        this.error = err?.message || 'Failed to fetch data';
        this.loading = false;
        this.cdr.detectChanges();
      }
    });
  }

  private calculateIndicators(): void {
    const indicatorConfigs: { name: string; window: number }[] = [];
    if (this.showSma) indicatorConfigs.push({ name: 'sma', window: this.smaWindow });
    if (this.showEma) indicatorConfigs.push({ name: 'ema', window: this.emaWindow });
    if (this.showRsi) indicatorConfigs.push({ name: 'rsi', window: this.rsiWindow });

    if (indicatorConfigs.length === 0) {
      this.indicators = [];
      this.loading = false;
      this.message = 'No indicators selected. Enable at least one above.';
      this.cdr.detectChanges();
      return;
    }

    this.marketDataService.calculateIndicators(
      this.ticker.toUpperCase(), this.fromDate, this.toDate,
      indicatorConfigs, this.timespan, this.multiplier
    ).subscribe({
      next: (result) => {
        this.indicators = result.indicators;
        this.message = result.message;
        this.loading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.error = err?.message || 'Failed to calculate indicators';
        this.loading = false;
        this.cdr.detectChanges();
      }
    });
  }
}
