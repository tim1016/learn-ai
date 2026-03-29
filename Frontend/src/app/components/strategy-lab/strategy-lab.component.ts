import {
  Component, signal, computed, inject,
  ChangeDetectionStrategy
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { forkJoin } from 'rxjs';
import { RouterModule } from '@angular/router';
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
    CommonModule, FormsModule, RouterModule, LineChartComponent,
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
  timeframe = signal('5m');
  strategyName = signal<'sma_crossover' | 'rsi_mean_reversion' | 'momentum_rsi_stochastic'>('sma_crossover');

  // Timeframe options for the dropdown
  readonly timeframeOptions = [
    { label: '1 min', value: '1m' },
    { label: '5 min', value: '5m' },
    { label: '15 min', value: '15m' },
    { label: '30 min', value: '30m' },
    { label: '1 hour', value: '1h' },
  ];

  // Derived timespan + multiplier from timeframe (for backward-compatible GraphQL calls)
  private get parsedTimeframe(): { timespan: string; multiplier: number } {
    const tf = this.timeframe();
    if (tf.endsWith('h')) {
      return { timespan: 'hour', multiplier: parseInt(tf) || 1 };
    }
    return { timespan: 'minute', multiplier: parseInt(tf) || 1 };
  }

  // Date validation
  minDate = getMinAllowedDate();

  // SMA params
  shortWindow = signal(10);
  longWindow = signal(30);

  // RSI params
  rsiWindow = signal(14);
  oversold = signal(30);
  overbought = signal(70);

  // Momentum RSI + Stochastic params
  momentumRsiLength = signal(14);
  momentumRsiLow = signal(40);
  momentumRsiHigh = signal(60);
  momentumFastMa = signal(20);
  momentumSlowMa = signal(50);
  momentumStochK = signal(14);
  momentumStochD = signal(3);
  momentumExitMinutes = signal(15);

  // State
  loading = signal(false);
  error = signal<string | null>(null);
  result = signal<BacktestResult | null>(null);

  // Computed: parameter JSON based on selected strategy
  parametersJson = computed(() => {
    switch (this.strategyName()) {
      case 'sma_crossover':
        return JSON.stringify({
          ShortWindow: this.shortWindow(),
          LongWindow: this.longWindow(),
        });
      case 'rsi_mean_reversion':
        return JSON.stringify({
          Window: this.rsiWindow(),
          Oversold: this.oversold(),
          Overbought: this.overbought(),
        });
      case 'momentum_rsi_stochastic':
        return JSON.stringify({
          RsiLength: this.momentumRsiLength(),
          RsiLow: this.momentumRsiLow(),
          RsiHigh: this.momentumRsiHigh(),
          FastMa: this.momentumFastMa(),
          SlowMa: this.momentumSlowMa(),
          StochK: this.momentumStochK(),
          StochD: this.momentumStochD(),
          ExitMinutesBefore: this.momentumExitMinutes(),
        });
    }
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

    const { timespan, multiplier } = this.parsedTimeframe;
    this.marketDataService.runBacktest(
      this.ticker().toUpperCase(),
      this.strategyName(),
      this.fromDate(),
      this.toDate(),
      timespan,
      multiplier,
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

    const { timespan, multiplier } = this.parsedTimeframe;
    this.marketDataService.getOrFetchStockAggregates(
      this.ticker().toUpperCase(),
      this.fromDate(),
      this.toDate(),
      timespan,
      multiplier,
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
    const { timespan, multiplier } = this.parsedTimeframe;

    const indicatorList = this.strategyName() === 'momentum_rsi_stochastic'
      ? [
          { name: 'sma', window: this.momentumFastMa() },
          { name: 'sma', window: this.momentumSlowMa() },
        ]
      : [
          { name: 'sma', window: this.shortWindow() },
          { name: 'sma', window: this.longWindow() },
        ];

    const indicators$ = this.marketDataService.calculateIndicators(
      ticker, from, to,
      indicatorList,
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
    // Ensure UTC interpretation — backend stores all timestamps as UTC
    const hasTimezone = iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso);
    const d = new Date(hasTimezone ? iso : iso + 'Z');
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
      timeZoneName: 'short',
    });
  }
}
