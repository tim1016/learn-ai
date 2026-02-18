import { Component, OnInit, ChangeDetectionStrategy, computed, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, tap } from 'rxjs';
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
  styleUrls: ['./market-data.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class MarketDataComponent implements OnInit {
  // Form inputs - signals for reactive updates
  ticker = signal('AAPL');
  fromDate = signal('');
  toDate = signal('');
  timespan = signal('day');
  multiplier = signal(1);
  minDate = getMinAllowedDate();

  // State signals
  loading = signal(false);
  error = signal<string | null>(null);
  aggregates = signal<StockAggregate[]>([]);
  summary = signal<AggregatesSummary | null>(null);

  holidays = signal<MarketHolidayEvent[]>([]);
  holidaysLoading = signal(false);

  // Computed: sorted aggregates
  sortedAggregates = computed(() =>
    [...this.aggregates()].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    )
  );

  private route = inject(ActivatedRoute);
  private marketMonitor = inject(MarketMonitorService);
  private marketDataService = inject(MarketDataService);

  constructor() {
    // Load holidays on component init
    this.loadHolidays();
  }

  ngOnInit(): void {
    const params = this.route.snapshot.queryParams;

    const today = new Date();
    const threeMonthsAgo = new Date(today);
    threeMonthsAgo.setMonth(threeMonthsAgo.getMonth() - 3);

    this.toDate.set(params['toDate'] || today.toISOString().split('T')[0]);
    this.fromDate.set(params['fromDate'] || threeMonthsAgo.toISOString().split('T')[0]);

    if (params['ticker']) {
      this.ticker.set(params['ticker']);
    }
    if (params['timespan']) {
      this.timespan.set(params['timespan']);
    }

    // Auto-fetch if ticker was provided via query params
    if (params['ticker']) {
      this.fetchData();
    }
  }

  onCalendarDateSelect(date: Date): void {
    this.toDate.set(date.toISOString().split('T')[0]);
  }

  fetchData(): void {
    if (!this.ticker()) {
      this.error.set('Please enter a ticker symbol');
      return;
    }

    const dateError = validateDateRange(this.fromDate(), this.toDate());
    if (dateError) {
      this.error.set(dateError);
      return;
    }

    this.loading.set(true);
    this.error.set(null);
    this.aggregates.set([]);
    this.summary.set(null);

    console.log('[STEP 1 - Component] fetchData called:', {
      ticker: this.ticker().toUpperCase(),
      from: this.fromDate(),
      to: this.toDate(),
      timespan: this.timespan(),
      multiplier: this.multiplier()
    });

    this.marketDataService.getOrFetchStockAggregates(
      this.ticker().toUpperCase(),
      this.fromDate(),
      this.toDate(),
      this.timespan(),
      this.multiplier()
    )
      .pipe(
        tap((result) => {
          console.log('[STEP 2 - Component] GraphQL response received:', {
            ticker: result?.ticker,
            aggregatesCount: result?.aggregates?.length ?? 0,
            hasSummary: !!result?.summary,
            firstBar: result?.aggregates?.[0],
            summary: result?.summary
          });
          this.aggregates.set([...result.aggregates].sort(
            (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
          ));
          this.summary.set(result.summary);
          this.loading.set(false);
        }),
        catchError((err) => {
          console.error('[STEP 2 - Component] GraphQL ERROR:', {
            message: err?.message,
            networkError: err?.networkError,
            graphQLErrors: err?.graphQLErrors,
            fullError: err
          });
          this.error.set(err?.message || 'Failed to fetch data');
          this.loading.set(false);
          return of(null);
        }),
        takeUntilDestroyed()
      )
      .subscribe();
  }

  private loadHolidays(): void {
    this.holidaysLoading.set(true);
    this.marketMonitor.getHolidays(20)
      .pipe(
        tap((events) => {
          this.holidays.set(events);
          this.holidaysLoading.set(false);
        }),
        catchError(() => {
          this.holidaysLoading.set(false);
          return of([]);
        }),
        takeUntilDestroyed()
      )
      .subscribe();
  }
}
