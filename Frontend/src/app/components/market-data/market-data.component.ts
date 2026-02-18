import { Component, OnInit, ChangeDetectorRef, inject } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MarketDataService } from '../../services/market-data.service';
import { MarketMonitorService } from '../../services/market-monitor.service';
import { StockAggregate, AggregatesSummary } from '../../graphql/types';
import { MarketHolidayEvent } from '../../models/market-monitor';
import { validateDateRange, getMinAllowedDate } from '../../utils/date-validation';
import { CandlestickChartComponent } from './candlestick-chart/candlestick-chart.component';
import { LineChartComponent } from './line-chart/line-chart.component';
import { VolumeChartComponent } from './volume-chart/volume-chart.component';
import { SummaryStatsComponent } from './summary-stats/summary-stats.component';
import { MarketCalendarComponent } from '../market-calendar/market-calendar.component';
import { TableModule } from 'primeng/table';

@Component({
  selector: 'app-market-data',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    CandlestickChartComponent, LineChartComponent,
    VolumeChartComponent, SummaryStatsComponent,
    MarketCalendarComponent,
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
  minDate = getMinAllowedDate();

  loading = false;
  error: string | null = null;
  aggregates: StockAggregate[] = [];
  summary: AggregatesSummary | null = null;

  holidays: MarketHolidayEvent[] = [];
  holidaysLoading = false;

  private cdr = inject(ChangeDetectorRef);
  private route = inject(ActivatedRoute);
  private marketMonitor = inject(MarketMonitorService);

  constructor(private marketDataService: MarketDataService) {}

  ngOnInit(): void {
    const params = this.route.snapshot.queryParams;

    const today = new Date();
    const threeMonthsAgo = new Date(today);
    threeMonthsAgo.setMonth(threeMonthsAgo.getMonth() - 3);
    this.toDate = params['toDate'] || today.toISOString().split('T')[0];
    this.fromDate = params['fromDate'] || threeMonthsAgo.toISOString().split('T')[0];

    if (params['ticker']) {
      this.ticker = params['ticker'];
    }
    if (params['timespan']) {
      this.timespan = params['timespan'];
    }

    // Fetch market holidays for the calendar widget
    this.holidaysLoading = true;
    this.marketMonitor.getHolidays(20).subscribe({
      next: (events) => {
        this.holidays = events;
        this.holidaysLoading = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.holidaysLoading = false;
        this.cdr.detectChanges();
      }
    });

    // Auto-fetch if ticker was provided via query params
    if (params['ticker']) {
      this.fetchData();
    }
  }

  onCalendarDateSelect(date: Date): void {
    this.toDate = date.toISOString().split('T')[0];
  }

  fetchData(): void {
    if (!this.ticker) {
      this.error = 'Please enter a ticker symbol';
      return;
    }

    const dateError = validateDateRange(this.fromDate, this.toDate);
    if (dateError) {
      this.error = dateError;
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
