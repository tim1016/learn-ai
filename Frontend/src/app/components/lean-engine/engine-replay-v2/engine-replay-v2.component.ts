import {
  Component, ChangeDetectionStrategy, inject, input, signal, computed, effect,
} from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Observable, forkJoin, map } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { MarketDataService } from '../../../services/market-data.service';
import { BacktestTrade } from '../../../graphql/types';
import { StudyListItem } from '../engine-history/engine-history.component';
import { ReplayEngineV2Service } from './services/replay-engine-v2.service';
import { ReplayChartV2Component } from './replay-chart-v2/replay-chart-v2.component';
import { ReplayControlsV2Component } from './replay-controls-v2/replay-controls-v2.component';
import { SignalStripComponent } from './signal-strip/signal-strip.component';
import { TradeFlashComponent } from './trade-flash/trade-flash.component';

interface StudyDetailTrade {
  tradeType: string;
  entryTimestamp: string;
  exitTimestamp: string;
  entryPrice: number;
  exitPrice: number;
  // Minimal API's default camelCase policy lowercases only the first char,
  // so `PnL` → `pnL` and `CumulativePnL` → `cumulativePnL`.
  pnL: number;
  cumulativePnL: number;
  signalReason: string | null;
}

interface StudyDetailResponse {
  id: number;
  trades?: StudyDetailTrade[];
}

@Component({
  selector: 'app-engine-replay-v2',
  standalone: true,
  imports: [
    DatePipe, DecimalPipe,
    ReplayChartV2Component, ReplayControlsV2Component,
    SignalStripComponent, TradeFlashComponent,
  ],
  providers: [ReplayEngineV2Service],
  templateUrl: './engine-replay-v2.component.html',
  styleUrls: ['./engine-replay-v2.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EngineReplayV2Component {
  private readonly marketData = inject(MarketDataService);
  private readonly http = inject(HttpClient);
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

  /** Full trade history, always visible. Each row is tagged by status so the
   *  UI can style reached/open/upcoming differently and highlight the
   *  currently-open trade with a live floating P&L. */
  readonly tradeLog = computed(() => {
    const now = this.svc.currentMs();
    const active = this.svc.activePosition();
    const floating = this.svc.position();
    return this.svc.trades().map(t => {
      const isOpen = active !== null && active.tradeNumber === t.tradeNumber;
      const entryReached = t.entryMs <= now;
      const exitReached = t.exitMs <= now;
      const status: 'upcoming' | 'open' | 'closed' = !entryReached
        ? 'upcoming'
        : isOpen
        ? 'open'
        : 'closed';
      return {
        tradeNumber: t.tradeNumber,
        tradeType: t.tradeType,
        entryTimestamp: t.entryTimestamp,
        entryPrice: t.entryPrice,
        exitTimestamp: t.exitTimestamp,
        exitPrice: t.exitPrice,
        // Live floating P&L for the currently-open trade; final P&L once closed.
        pnl: isOpen ? (floating.floatingPnl ?? 0) : t.pnl,
        status,
        entryReached,
        exitReached,
      };
    });
  });

  constructor() {
    effect(() => {
      const s = this.study();
      if (s) this.loadForStudy(s);
      else this.svc.reset();
    });
  }

  isShort(tradeType: string): boolean {
    return /short/i.test(tradeType);
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
          // Use the already-persisted trades from the study instead of re-running
          // the backtest. Re-running routed through the legacy .NET rule-based
          // switch, which doesn't know most engine strategies and silently
          // returned zero trades — so the replay showed "NO TRADES" even when
          // Results had them.
          const study$ = this.fetchStudyTrades(s.id);
          if (indicators$) {
            forkJoin({ indicators: indicators$, trades: study$ }).subscribe({
              next: ({ indicators, trades }) => {
                this.svc.load({
                  bars: aggregates,
                  trades,
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
            study$.subscribe({
              next: (trades) => {
                this.svc.load({ bars: aggregates, trades, indicators: [] });
                this.dataLoaded.set(true);
                this.overlayLoading.set(false);
              },
              error: (err) => {
                this.error.set(err?.message ?? 'Failed to load study trades');
                this.overlayLoading.set(false);
              },
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

  /** Fetch the persisted trades for a study from the backend's studies API
   *  and map them onto the BacktestTrade shape the replay engine expects.
   *  Minimal API default JSON casing is camelCase, but the `PnL` property
   *  serializes as `pnL` (lowercase first char only) — hence the remap. */
  private fetchStudyTrades(studyId: number): Observable<BacktestTrade[]> {
    const backendBase = (environment.backendUrl ?? 'http://localhost:5000').replace(/\/graphql$/, '');
    return this.http
      .get<StudyDetailResponse>(`${backendBase}/api/studies/${studyId}`)
      .pipe(map(detail => (detail.trades ?? []).map(t => ({
        tradeType: t.tradeType,
        entryTimestamp: t.entryTimestamp,
        exitTimestamp: t.exitTimestamp,
        entryPrice: t.entryPrice,
        exitPrice: t.exitPrice,
        pnl: t.pnL,
        cumulativePnl: t.cumulativePnL,
        signalReason: t.signalReason ?? '',
      }))));
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
    if (name === 'ema_crossover' || name === 'ema_crossover_rsi') {
      return [
        { name: 'ema', window: Number(params['fast_ema_period'] ?? 5) },
        { name: 'ema', window: Number(params['slow_ema_period'] ?? 10) },
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
