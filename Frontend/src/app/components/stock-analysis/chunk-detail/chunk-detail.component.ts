import { Component, inject, signal, computed, ChangeDetectionStrategy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../../services/market-data.service';
import { StockAggregate } from '../../../graphql/types';
import { LineChartComponent } from '../../market-data/line-chart/line-chart.component';
import { AtmMethod } from '../models';

interface DaySummary {
  date: string;
  barCount: number;
  open: number;
  close: number;
  high: number;
  low: number;
  totalVolume: number;
}

@Component({
  selector: 'app-chunk-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, LineChartComponent],
  templateUrl: './chunk-detail.component.html',
  styleUrls: ['./chunk-detail.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChunkDetailComponent implements OnInit {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private marketDataService = inject(MarketDataService);

  ticker = signal('');
  fromDate = signal('');
  toDate = signal('');
  atmMethod = signal<AtmMethod>('previousClose');

  stockBars = signal<StockAggregate[]>([]);
  loading = signal(true);
  error = signal<string | null>(null);

  resolution = signal<'minute' | 'day'>('minute');

  sortedBars = computed(() =>
    [...this.stockBars()].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    )
  );

  chartBars = computed<StockAggregate[]>(() => {
    const bars = this.sortedBars();
    if (this.resolution() === 'minute') return bars;

    // Aggregate minute bars into daily bars
    const dayMap = new Map<string, StockAggregate[]>();
    for (const bar of bars) {
      const day = bar.timestamp.split('T')[0];
      if (!dayMap.has(day)) dayMap.set(day, []);
      dayMap.get(day)!.push(bar);
    }

    return [...dayMap.keys()].sort().map(date => {
      const dayBars = dayMap.get(date)!;
      const sorted = [...dayBars].sort(
        (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      );
      return {
        ...sorted[0],
        timestamp: date + 'T00:00:00',
        open: sorted[0].open,
        close: sorted[sorted.length - 1].close,
        high: Math.max(...dayBars.map(b => b.high)),
        low: Math.min(...dayBars.map(b => b.low)),
        volume: dayBars.reduce((sum, b) => sum + b.volume, 0),
      };
    });
  });

  tradingDays = computed<DaySummary[]>(() => {
    const bars = this.sortedBars();
    const dayMap = new Map<string, StockAggregate[]>();
    for (const bar of bars) {
      const day = bar.timestamp.split('T')[0];
      if (!dayMap.has(day)) dayMap.set(day, []);
      dayMap.get(day)!.push(bar);
    }

    return [...dayMap.keys()].sort().map(date => {
      const dayBars = dayMap.get(date)!;
      const sorted = [...dayBars].sort(
        (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      );
      return {
        date,
        barCount: dayBars.length,
        open: sorted[0].open,
        close: sorted[sorted.length - 1].close,
        high: Math.max(...dayBars.map(b => b.high)),
        low: Math.min(...dayBars.map(b => b.low)),
        totalVolume: dayBars.reduce((sum, b) => sum + b.volume, 0),
      };
    });
  });

  ngOnInit(): void {
    const params = this.route.snapshot.params;
    const queryParams = this.route.snapshot.queryParams;

    this.ticker.set(params['ticker'] ?? '');
    this.fromDate.set(params['fromDate'] ?? '');
    this.toDate.set(params['toDate'] ?? '');
    if (queryParams['atm']) {
      this.atmMethod.set(queryParams['atm'] as AtmMethod);
    }

    this.loadData();
  }

  navigateToDay(date: string): void {
    this.router.navigate(
      ['/stock-analysis/day', this.ticker(), date],
      { queryParams: { atm: this.atmMethod() } }
    );
  }

  dayChangePercent(day: DaySummary): number {
    if (day.open === 0) return 0;
    return ((day.close - day.open) / day.open) * 100;
  }

  private async loadData(): Promise<void> {
    const t = this.ticker();
    const from = this.fromDate();
    const to = this.toDate();
    if (!t || !from || !to) {
      this.error.set('Missing ticker or date range');
      this.loading.set(false);
      return;
    }

    try {
      const result = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(t, from, to, 'minute', 1)
      );
      this.stockBars.set(result.aggregates);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    }

    this.loading.set(false);
  }
}
