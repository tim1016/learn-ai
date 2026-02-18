import {
  Component, signal, computed, inject,
  ChangeDetectionStrategy
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { MarketDataService } from '../../services/market-data.service';
import { BacktestResult, StockAggregate } from '../../graphql/types';
import { LineChartComponent } from '../market-data/line-chart/line-chart.component';
import { BacktestSvgChartComponent } from './charts/backtest-svg-chart.component';
import { BacktestPrimengChartComponent } from './charts/backtest-primeng-chart.component';
import { validateDateRange, getMinAllowedDate } from '../../utils/date-validation';

export type ChartType = 'lightweight' | 'svg' | 'primeng';

@Component({
  selector: 'app-strategy-lab',
  standalone: true,
  imports: [
    CommonModule, FormsModule, LineChartComponent,
    BacktestSvgChartComponent, BacktestPrimengChartComponent,
  ],
  templateUrl: './strategy-lab.component.html',
  styleUrls: ['./strategy-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabComponent {
  private marketDataService = inject(MarketDataService);

  // Chart type selector
  chartType = signal<ChartType>('lightweight');

  // Form inputs
  ticker = signal('AAPL');
  fromDate = signal('2025-01-02');
  toDate = signal('2025-02-01');
  timespan = signal('minute');
  multiplier = signal(1);
  strategyName = signal<'sma_crossover' | 'rsi_mean_reversion'>('sma_crossover');

  // Date validation
  minDate = getMinAllowedDate();

  // SMA params
  shortWindow = signal(10);
  longWindow = signal(30);

  // RSI params
  rsiWindow = signal(14);
  oversold = signal(30);
  overbought = signal(70);

  // State
  loading = signal(false);
  error = signal<string | null>(null);
  result = signal<BacktestResult | null>(null);

  // Computed: parameter JSON based on selected strategy
  parametersJson = computed(() => {
    if (this.strategyName() === 'sma_crossover') {
      return JSON.stringify({
        ShortWindow: this.shortWindow(),
        LongWindow: this.longWindow(),
      });
    }
    return JSON.stringify({
      Window: this.rsiWindow(),
      Oversold: this.oversold(),
      Overbought: this.overbought(),
    });
  });

  // Computed: equity curve data for the line chart
  equityCurve = computed<StockAggregate[]>(() => {
    const r = this.result();
    if (!r?.trades?.length) return [];
    return r.trades.map((t, i) => ({
      id: i,
      tickerId: 0,
      open: t.cumulativePnl,
      high: t.cumulativePnl,
      low: t.cumulativePnl,
      close: t.cumulativePnl,
      volume: 0,
      volumeWeightedAveragePrice: null,
      timestamp: t.exitTimestamp,
      timespan: 'trade',
      multiplier: 1,
      transactionCount: null,
    }));
  });

  // Computed: summary stats
  winRate = computed(() => {
    const r = this.result();
    if (!r || r.totalTrades === 0) return 0;
    return Math.round((r.winningTrades / r.totalTrades) * 100);
  });

  avgPnl = computed(() => {
    const r = this.result();
    if (!r || r.totalTrades === 0) return 0;
    return r.totalPnL / r.totalTrades;
  });

  runBacktest(): void {
    const dateError = validateDateRange(this.fromDate(), this.toDate());
    if (dateError) {
      this.error.set(dateError);
      return;
    }

    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    this.marketDataService.runBacktest(
      this.ticker().toUpperCase(),
      this.strategyName(),
      this.fromDate(),
      this.toDate(),
      this.timespan(),
      this.multiplier(),
      this.parametersJson()
    ).subscribe({
      next: (res) => {
        if (!res.success) {
          this.error.set(res.error || 'Backtest failed');
        } else {
          this.result.set(res);
        }
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err.message || 'Request failed');
        this.loading.set(false);
      },
    });
  }

  formatPrice(val: number): string {
    return val.toFixed(2);
  }

  formatTimestamp(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  }
}
