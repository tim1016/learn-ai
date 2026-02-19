import {
  Component, signal, computed, inject,
  ChangeDetectionStrategy
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { forkJoin } from 'rxjs';
import { MarketDataService } from '../../services/market-data.service';
import { BacktestResult, StockAggregate } from '../../graphql/types';
import { LineChartComponent } from '../market-data/line-chart/line-chart.component';
import { BacktestSvgChartComponent } from './charts/backtest-svg-chart.component';
import { BacktestPrimengChartComponent } from './charts/backtest-primeng-chart.component';
import { ReplayControlsComponent } from './replay-controls/replay-controls.component';
import { ReplayChartComponent } from './replay-chart/replay-chart.component';
import { ReplayEngineService } from '../../services/replay-engine.service';
import { ReplayIndicatorService } from '../../services/replay-indicator.service';
import { ReplayStrategyService } from '../../services/replay-strategy.service';
import { validateDateRange, getMinAllowedDate } from '../../utils/date-validation';

export type ChartType = 'lightweight' | 'svg' | 'primeng';
export type LabMode = 'backtest' | 'replay';

@Component({
  selector: 'app-strategy-lab',
  standalone: true,
  imports: [
    CommonModule, FormsModule, LineChartComponent,
    BacktestSvgChartComponent, BacktestPrimengChartComponent,
    ReplayControlsComponent, ReplayChartComponent,
  ],
  templateUrl: './strategy-lab.component.html',
  styleUrls: ['./strategy-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabComponent {
  private marketDataService = inject(MarketDataService);
  readonly replayEngine = inject(ReplayEngineService);
  readonly replayIndicators = inject(ReplayIndicatorService);
  readonly replayStrategy = inject(ReplayStrategyService);

  // Mode toggle
  mode = signal<LabMode>('backtest');

  // Replay state
  replayLoading = signal(false);
  replayDataLoaded = signal(false);
  replayOverlayLoading = signal(false);

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

  // Computed: replay equity curve from completed trades
  replayEquityCurve = computed<StockAggregate[]>(() => {
    const completed = this.replayStrategy.completedTrades();
    if (!completed.length) return [];
    return completed.map((t, i) => ({
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

  loadReplayData(): void {
    const dateError = validateDateRange(this.fromDate(), this.toDate());
    if (dateError) {
      this.error.set(dateError);
      return;
    }

    this.replayLoading.set(true);
    this.error.set(null);
    this.replayDataLoaded.set(false);
    this.replayEngine.reset();
    this.replayIndicators.reset();
    this.replayStrategy.reset();

    this.marketDataService.getOrFetchStockAggregates(
      this.ticker().toUpperCase(),
      this.fromDate(),
      this.toDate(),
      this.timespan(),
      this.multiplier(),
    ).subscribe({
      next: (res) => {
        const aggregates = res.aggregates ?? [];
        if (aggregates.length === 0) {
          this.error.set('No data found for the specified range. Fetch data on the Market Data page first.');
          this.replayLoading.set(false);
          return;
        }

        this.replayEngine.load(aggregates);
        this.replayDataLoaded.set(true);
        this.replayLoading.set(false);

        // Load indicators and backtest trades as overlays
        this.loadReplayOverlays();
      },
      error: (err) => {
        this.error.set(err.message || 'Failed to load replay data');
        this.replayLoading.set(false);
      },
    });
  }

  private loadReplayOverlays(): void {
    this.replayOverlayLoading.set(true);
    const ticker = this.ticker().toUpperCase();
    const from = this.fromDate();
    const to = this.toDate();
    const timespan = this.timespan();
    const multiplier = this.multiplier();

    const indicators$ = this.marketDataService.calculateIndicators(
      ticker, from, to,
      [
        { name: 'sma', window: this.shortWindow() },
        { name: 'sma', window: this.longWindow() },
      ],
      timespan, multiplier,
    );

    const backtest$ = this.marketDataService.runBacktest(
      ticker, this.strategyName(), from, to,
      timespan, multiplier, this.parametersJson(),
    );

    forkJoin({ indicators: indicators$, backtest: backtest$ }).subscribe({
      next: ({ indicators, backtest }) => {
        if (indicators.success && indicators.indicators) {
          this.replayIndicators.loadIndicators(indicators.indicators);
        }
        if (backtest.success && backtest.trades) {
          this.replayStrategy.loadTrades(backtest.trades);
        }
        this.replayOverlayLoading.set(false);
      },
      error: () => {
        // Overlays are optional — don't block replay if they fail
        this.replayOverlayLoading.set(false);
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
