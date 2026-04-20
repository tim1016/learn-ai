import {
  Component, ChangeDetectionStrategy, inject, input, signal, computed, effect,
} from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { forkJoin } from 'rxjs';
import { MarketDataService } from '../../../services/market-data.service';
import { StudyListItem } from '../engine-history/engine-history.component';
import { ReplayEngineV2Service } from './services/replay-engine-v2.service';
import { ReplayChartV2Component } from './replay-chart-v2/replay-chart-v2.component';
import { ReplayControlsV2Component } from './replay-controls-v2/replay-controls-v2.component';
import { SignalStripComponent } from './signal-strip/signal-strip.component';
import { PositionHudComponent } from './position-hud/position-hud.component';
import { TradeFlashComponent } from './trade-flash/trade-flash.component';

@Component({
  selector: 'app-engine-replay-v2',
  standalone: true,
  imports: [
    DatePipe, DecimalPipe,
    ReplayChartV2Component, ReplayControlsV2Component,
    SignalStripComponent, PositionHudComponent, TradeFlashComponent,
  ],
  providers: [ReplayEngineV2Service],
  templateUrl: './engine-replay-v2.component.html',
  styleUrls: ['./engine-replay-v2.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EngineReplayV2Component {
  private readonly marketData = inject(MarketDataService);
  readonly svc = inject(ReplayEngineV2Service);

  readonly study = input<StudyListItem | null>(null);

  readonly loading = signal(false);
  readonly overlayLoading = signal(false);
  readonly error = signal<string | null>(null);
  readonly dataLoaded = signal(false);

  readonly header = computed(() => {
    const s = this.study();
    if (!s) return null;
    return {
      symbol: s.symbol,
      strategy: s.strategyName,
      range: `${s.startDate} → ${s.endDate}`,
      timespan: s.timespan,
    };
  });

  readonly trades = this.svc.trades;
  readonly currentMs = this.svc.currentMs;

  constructor() {
    effect(() => {
      const s = this.study();
      if (s) this.loadForStudy(s);
      else this.svc.reset();
    });
  }

  onRowClick(entryTs: string): void {
    const entryMs = new Date(entryTs).getTime();
    const bars = this.svc.bars();
    // Binary-search the first bar whose ts >= entryMs
    let lo = 0, hi = bars.length - 1, result = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (new Date(bars[mid].timestamp).getTime() >= entryMs) {
        result = mid;
        hi = mid - 1;
      } else {
        lo = mid + 1;
      }
    }
    this.svc.seekTo(result);
  }

  private loadForStudy(s: StudyListItem): void {
    this.loading.set(true);
    this.overlayLoading.set(false);
    this.error.set(null);
    this.dataLoaded.set(false);
    this.svc.reset();

    const ticker = s.symbol.toUpperCase();
    const { timespan, multiplier } = this.parseTimespan(s.timespan);

    this.marketData
      .getOrFetchStockAggregates(ticker, s.startDate, s.endDate, timespan, multiplier)
      .subscribe({
        next: (bars) => {
          const aggregates = bars.aggregates ?? [];
          if (aggregates.length === 0) {
            this.error.set('No bars found for this study range. Fetch on the Market Data page.');
            this.loading.set(false);
            return;
          }
          this.loading.set(false);
          this.overlayLoading.set(true);
          const indicatorList = this.indicatorsForStrategy(s);
          const indicators$ = indicatorList.length
            ? this.marketData.calculateIndicators(ticker, s.startDate, s.endDate, indicatorList, timespan, multiplier)
            : null;
          const backtest$ = this.marketData.runBacktest(
            ticker, s.strategyName, s.startDate, s.endDate, timespan, multiplier,
            s.parameters || '{}',
          );
          if (indicators$) {
            forkJoin({ indicators: indicators$, backtest: backtest$ }).subscribe({
              next: ({ indicators, backtest }) => {
                this.svc.load({
                  bars: aggregates,
                  trades: (backtest.success && backtest.trades) ? backtest.trades : [],
                  indicators: (indicators.success && indicators.indicators) ? indicators.indicators : [],
                });
                this.dataLoaded.set(true);
                this.overlayLoading.set(false);
              },
              error: (err) => {
                this.error.set(err?.message ?? 'Failed to load overlays');
                this.overlayLoading.set(false);
              },
            });
          } else {
            backtest$.subscribe({
              next: (backtest) => {
                this.svc.load({
                  bars: aggregates,
                  trades: (backtest.success && backtest.trades) ? backtest.trades : [],
                  indicators: [],
                });
                this.dataLoaded.set(true);
                this.overlayLoading.set(false);
              },
              error: () => this.overlayLoading.set(false),
            });
          }
        },
        error: (err) => {
          this.error.set(err?.message ?? 'Failed to load replay data');
          this.loading.set(false);
        },
      });
  }

  private parseTimespan(raw: string): { timespan: string; multiplier: number } {
    return { timespan: (raw || 'minute').toLowerCase(), multiplier: 1 };
  }

  private indicatorsForStrategy(s: StudyListItem): { name: string; window: number }[] {
    const name = s.strategyName;
    const params = this.parseParams(s.parameters);
    if (name === 'SpyEmaCrossover' || name === 'spy_ema_crossover') {
      return [
        { name: 'ema', window: Number(params['fast_period'] ?? 5) },
        { name: 'ema', window: Number(params['slow_period'] ?? 10) },
      ];
    }
    if (name === 'SmaCrossover' || name === 'sma_crossover') {
      return [
        { name: 'sma', window: Number(params['shortWindow'] ?? 10) },
        { name: 'sma', window: Number(params['longWindow'] ?? 30) },
      ];
    }
    if (name === 'RsiMeanReversion' || name === 'rsi_mean_reversion') {
      return [{ name: 'rsi', window: Number(params['rsiWindow'] ?? 14) }];
    }
    return [];
  }

  private parseParams(raw: string): Record<string, unknown> {
    if (!raw) return {};
    try { return JSON.parse(raw); } catch { return {}; }
  }
}
