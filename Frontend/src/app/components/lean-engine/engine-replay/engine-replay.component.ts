import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { forkJoin } from 'rxjs';
import { MarketDataService } from '../../../services/market-data.service';
import { ReplayEngineService } from '../../../services/replay-engine.service';
import { ReplayIndicatorService } from '../../../services/replay-indicator.service';
import { ReplayStrategyService } from '../../../services/replay-strategy.service';
import { ReplayChartComponent } from '../../strategy-lab/replay-chart/replay-chart.component';
import { ReplayControlsComponent } from '../../strategy-lab/replay-controls/replay-controls.component';
import { BacktestTrade } from '../../../graphql/types';
import { StudyListItem } from '../engine-history/engine-history.component';

type BacktestTradeLike = BacktestTrade & { tradeNumber: number };

interface TradeLedgerRow {
  tradeNumber: number;
  type: string;
  entryTime: string;
  entryPrice: number;
  exitTime: string;
  exitPrice: number;
  pnl: number;
  signalReason: string;
  status: 'open' | 'closed';
}

@Component({
  selector: 'app-engine-replay',
  standalone: true,
  imports: [CommonModule, ReplayChartComponent, ReplayControlsComponent],
  templateUrl: './engine-replay.component.html',
  styleUrls: ['./engine-replay.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EngineReplayComponent {
  private readonly marketData = inject(MarketDataService);
  readonly replayEngine = inject(ReplayEngineService);
  readonly replayIndicators = inject(ReplayIndicatorService);
  readonly replayStrategy = inject(ReplayStrategyService);

  readonly study = input<StudyListItem | null>(null);

  readonly loading = signal(false);
  readonly overlayLoading = signal(false);
  readonly error = signal<string | null>(null);
  readonly dataLoaded = signal(false);

  readonly visibleBars = this.replayEngine.visibleBars;
  readonly currentBar = this.replayEngine.currentBar;
  readonly visibleTrades = this.replayStrategy.visibleTrades;
  readonly activePosition = this.replayStrategy.activePosition;
  readonly visibleIndicators = this.replayIndicators.visibleIndicators;

  /** Indicator name + current value at the current bar (latest point ≤ now). */
  readonly currentIndicatorValues = computed<{ label: string; value: number | null }[]>(() => {
    return this.visibleIndicators().map((s) => {
      const last = s.data.length ? s.data[s.data.length - 1] : null;
      return {
        label: `${s.name.toUpperCase()}(${s.window})`,
        value: last?.value ?? null,
      };
    });
  });

  /** If the current bar is an entry or exit bar, announce it. */
  readonly currentEvent = computed<{ kind: 'entry' | 'exit'; trade: BacktestTradeLike } | null>(() => {
    const bar = this.currentBar();
    if (!bar) return null;
    const nowMs = new Date(bar.timestamp).getTime();
    const trades = this.replayStrategy.allTrades();
    for (let i = 0; i < trades.length; i++) {
      const t = trades[i];
      if (new Date(t.entryTimestamp).getTime() === nowMs) {
        return { kind: 'entry', trade: { ...t, tradeNumber: i + 1 } };
      }
      if (new Date(t.exitTimestamp).getTime() === nowMs) {
        return { kind: 'exit', trade: { ...t, tradeNumber: i + 1 } };
      }
    }
    return null;
  });

  /** Visible trades, oldest → newest, annotated with status + trade number.
   *  Status matches on (entry,exit) pair rather than reference identity so the
   *  check survives any future cloning in the upstream signal. */
  readonly tradeLedger = computed<TradeLedgerRow[]>(() => {
    const active = this.activePosition();
    const activeKey = active ? `${active.entryTimestamp}|${active.exitTimestamp}` : null;
    return this.visibleTrades().map((t, idx) => ({
      tradeNumber: idx + 1,
      type: t.tradeType,
      entryTime: t.entryTimestamp,
      entryPrice: t.entryPrice,
      exitTime: t.exitTimestamp,
      exitPrice: t.exitPrice,
      pnl: t.pnl,
      signalReason: t.signalReason,
      status: activeKey === `${t.entryTimestamp}|${t.exitTimestamp}` ? 'open' : 'closed',
    }));
  });

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

  constructor() {
    effect(() => {
      const s = this.study();
      if (s) {
        this.loadForStudy(s);
      }
    });
  }

  private loadForStudy(s: StudyListItem): void {
    this.loading.set(true);
    this.error.set(null);
    this.dataLoaded.set(false);
    this.replayEngine.reset();
    this.replayIndicators.reset();
    this.replayStrategy.reset();

    const ticker = s.symbol.toUpperCase();
    const { timespan, multiplier } = this.parseTimespan(s.timespan);

    this.marketData
      .getOrFetchStockAggregates(ticker, s.startDate, s.endDate, timespan, multiplier)
      .subscribe({
        next: (res) => {
          const aggregates = res.aggregates ?? [];
          if (aggregates.length === 0) {
            this.error.set('No bars found for this study range. Fetch on the Market Data page.');
            this.loading.set(false);
            return;
          }
          this.replayEngine.load(aggregates);
          this.dataLoaded.set(true);
          this.loading.set(false);
          this.loadOverlays(s, ticker, timespan, multiplier);
        },
        error: (err) => {
          this.error.set(err?.message ?? 'Failed to load replay data');
          this.loading.set(false);
        },
      });
  }

  private loadOverlays(
    s: StudyListItem,
    ticker: string,
    timespan: string,
    multiplier: number,
  ): void {
    this.overlayLoading.set(true);
    const indicatorList = this.indicatorsForStrategy(s);

    const indicators$ = indicatorList.length
      ? this.marketData.calculateIndicators(ticker, s.startDate, s.endDate, indicatorList, timespan, multiplier)
      : null;

    const backtest$ = this.marketData.runBacktest(
      ticker,
      s.strategyName,
      s.startDate,
      s.endDate,
      timespan,
      multiplier,
      s.parameters || '{}',
    );

    if (indicators$) {
      forkJoin({ indicators: indicators$, backtest: backtest$ }).subscribe({
        next: ({ indicators, backtest }) => {
          if (indicators.success && indicators.indicators) {
            this.replayIndicators.loadIndicators(indicators.indicators);
          }
          if (backtest.success && backtest.trades) {
            this.replayStrategy.loadTrades(backtest.trades);
          }
          this.overlayLoading.set(false);
        },
        error: () => this.overlayLoading.set(false),
      });
    } else {
      backtest$.subscribe({
        next: (backtest) => {
          if (backtest.success && backtest.trades) {
            this.replayStrategy.loadTrades(backtest.trades);
          }
          this.overlayLoading.set(false);
        },
        error: () => this.overlayLoading.set(false),
      });
    }
  }

  private parseTimespan(raw: string): { timespan: string; multiplier: number } {
    // StudyListItem.timespan is a bare unit (e.g. "minute"); multiplier lives
    // in the Backend's Multiplier column but is not projected here yet.
    // Default multiplier=1 — matches the lean-engine default.
    const t = (raw || 'minute').toLowerCase();
    return { timespan: t, multiplier: 1 };
  }

  private indicatorsForStrategy(s: StudyListItem): { name: string; window: number }[] {
    // Mirrors the hard-coded canonical mapping in lean-engine.component.ts
    // strategyIndicators. When strategies expose metadata, replace with lookup.
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
    try {
      return JSON.parse(raw);
    } catch {
      return {};
    }
  }
}
