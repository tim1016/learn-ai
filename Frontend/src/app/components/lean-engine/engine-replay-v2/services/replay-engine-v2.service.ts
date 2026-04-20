import {
  Injectable, signal, computed, DestroyRef, inject,
} from '@angular/core';
import {
  StockAggregate, BacktestTrade, IndicatorSeries, IndicatorPoint,
} from '../../../../graphql/types';

export type PlaybackState = 'stopped' | 'playing' | 'paused';
export type Direction = 'forward' | 'reverse';
export type WindowSize = number | 'all';

export interface TradeWithNumber extends BacktestTrade {
  tradeNumber: number;
  entryMs: number;
  exitMs: number;
}

export interface RenderWindow {
  bars: StockAggregate[];
  startIndex: number;
  endIndex: number;
  indexInWindow: number;
}

export interface IndicatorWindowSeries {
  name: string;
  window: number;
  data: IndicatorPoint[];
}

export interface SignalCard {
  name: string;
  window: number;
  currentValue: number | null;
  entryValue: number | null;
  delta: number | null;
  sparkline: number[];
}

export interface HiddenSummary {
  leftCount: number;
  leftCumPnl: number;
  rightCount: number;
  rightCumPnl: number;
}

export interface PositionState {
  side: 'long' | 'short' | 'flat';
  entryPrice: number | null;
  entryTime: string | null;
  barsHeld: number;
  floatingPnl: number | null;
  floatingPnlPct: number | null;
  trade: TradeWithNumber | null;
}

export type FlashEvent =
  | { kind: 'exit'; trade: TradeWithNumber; timestamp: number }
  | { kind: 'unwind'; trade: TradeWithNumber; timestamp: number };

const BASE_INTERVAL_MS = 100;
const FLASH_DURATION_MS = 1500;
const DEFAULT_WINDOW = 200;

@Injectable()
export class ReplayEngineV2Service {
  private readonly destroyRef = inject(DestroyRef);
  private intervalId: ReturnType<typeof setInterval> | null = null;
  private flashTimeoutId: ReturnType<typeof setTimeout> | null = null;

  private readonly _bars = signal<StockAggregate[]>([]);
  private readonly _trades = signal<TradeWithNumber[]>([]);
  private readonly _indicators = signal<IndicatorSeries[]>([]);
  private readonly _currentIndex = signal(0);
  private readonly _playbackState = signal<PlaybackState>('stopped');
  private readonly _playbackSpeed = signal(1);
  private readonly _direction = signal<Direction>('forward');
  private readonly _windowSize = signal<WindowSize>(DEFAULT_WINDOW);
  private readonly _flashEvent = signal<FlashEvent | null>(null);

  readonly bars = this._bars.asReadonly();
  readonly trades = this._trades.asReadonly();
  readonly currentIndex = this._currentIndex.asReadonly();
  readonly playbackState = this._playbackState.asReadonly();
  readonly playbackSpeed = this._playbackSpeed.asReadonly();
  readonly direction = this._direction.asReadonly();
  readonly windowSize = this._windowSize.asReadonly();
  readonly flashEvent = this._flashEvent.asReadonly();

  readonly totalBars = computed(() => this._bars().length);

  readonly currentBar = computed(() => this._bars()[this._currentIndex()] ?? null);

  readonly currentMs = computed(() => {
    const b = this.currentBar();
    return b ? new Date(b.timestamp).getTime() : 0;
  });

  readonly progress = computed(() => {
    const total = this.totalBars();
    if (total <= 1) return 0;
    return this._currentIndex() / (total - 1);
  });

  readonly isAtStart = computed(() => this._currentIndex() === 0);

  readonly isAtEnd = computed(() => {
    const total = this.totalBars();
    if (total === 0) return true;
    return this._currentIndex() >= total - 1;
  });

  readonly renderWindow = computed<RenderWindow>(() => {
    const bars = this._bars();
    const idx = this._currentIndex();
    const size = this._windowSize();
    if (bars.length === 0) {
      return { bars: [], startIndex: 0, endIndex: 0, indexInWindow: 0 };
    }
    if (size === 'all') {
      return {
        bars,
        startIndex: 0,
        endIndex: bars.length - 1,
        indexInWindow: idx,
      };
    }
    // Right-anchored rolling window: the playhead always sits at the right
    // edge. Bars scroll in from the right as the index advances, and old bars
    // fall off the left once the cursor exceeds windowSize. This is the
    // classic DVR / market-replay feel.
    const end = idx;
    const start = Math.max(0, idx - size + 1);
    return {
      bars: bars.slice(start, end + 1),
      startIndex: start,
      endIndex: end,
      indexInWindow: end - start,
    };
  });

  readonly windowTrades = computed<TradeWithNumber[]>(() => {
    const win = this.renderWindow();
    const trades = this._trades();
    if (win.bars.length === 0) return [];
    const leftMs = new Date(win.bars[0].timestamp).getTime();
    const rightMs = new Date(win.bars[win.bars.length - 1].timestamp).getTime();
    const nowMs = this.currentMs();
    return trades.filter(t => {
      const entryInside = t.entryMs >= leftMs && t.entryMs <= rightMs;
      const exitInside = t.exitMs >= leftMs && t.exitMs <= rightMs && t.exitMs <= nowMs;
      const straddles = t.entryMs <= leftMs && t.exitMs >= rightMs;
      return entryInside || exitInside || straddles;
    });
  });

  readonly hiddenSummary = computed<HiddenSummary>(() => {
    const win = this.renderWindow();
    const trades = this._trades();
    const nowMs = this.currentMs();
    if (win.bars.length === 0) {
      return { leftCount: 0, leftCumPnl: 0, rightCount: 0, rightCumPnl: 0 };
    }
    const leftMs = new Date(win.bars[0].timestamp).getTime();
    const rightMs = new Date(win.bars[win.bars.length - 1].timestamp).getTime();

    let leftCount = 0, leftCumPnl = 0;
    let rightCount = 0, rightCumPnl = 0;
    for (const t of trades) {
      // Trade counts only once its exit has been "reached" by playback
      if (t.exitMs > nowMs) continue;
      if (t.exitMs < leftMs) { leftCount++; leftCumPnl += t.pnl; }
      else if (t.entryMs > rightMs) { rightCount++; rightCumPnl += t.pnl; }
    }
    return { leftCount, leftCumPnl, rightCount, rightCumPnl };
  });

  readonly activePosition = computed<TradeWithNumber | null>(() => {
    const nowMs = this.currentMs();
    if (!nowMs) return null;
    return this._trades().find(t => t.entryMs <= nowMs && t.exitMs > nowMs) ?? null;
  });

  readonly position = computed<PositionState>(() => {
    const trade = this.activePosition();
    const bar = this.currentBar();
    if (!trade || !bar) {
      return {
        side: 'flat',
        entryPrice: null, entryTime: null,
        barsHeld: 0,
        floatingPnl: null, floatingPnlPct: null,
        trade: null,
      };
    }
    const bars = this._bars();
    const idx = this._currentIndex();
    // Count bars from entry timestamp to current
    let barsHeld = 0;
    for (let i = idx; i >= 0; i--) {
      if (new Date(bars[i].timestamp).getTime() < trade.entryMs) break;
      barsHeld++;
    }
    const side: 'long' | 'short' = /short/i.test(trade.tradeType) ? 'short' : 'long';
    const priceDiff = bar.close - trade.entryPrice;
    const floatingPnl = side === 'long' ? priceDiff : -priceDiff;
    const floatingPnlPct = trade.entryPrice !== 0 ? (floatingPnl / trade.entryPrice) * 100 : 0;
    return {
      side,
      entryPrice: trade.entryPrice,
      entryTime: trade.entryTimestamp,
      barsHeld,
      floatingPnl,
      floatingPnlPct,
      trade,
    };
  });

  readonly visibleIndicatorsWindow = computed<IndicatorWindowSeries[]>(() => {
    const win = this.renderWindow();
    const series = this._indicators();
    const nowMs = this.currentMs();
    if (!nowMs || win.bars.length === 0) return [];
    const leftMs = new Date(win.bars[0].timestamp).getTime();
    return series.map(s => ({
      name: s.name,
      window: s.window,
      data: s.data.filter(p =>
        p.value !== null && p.timestamp >= leftMs && p.timestamp <= nowMs
      ),
    }));
  });

  readonly signalCards = computed<SignalCard[]>(() => {
    const series = this._indicators();
    const nowMs = this.currentMs();
    const active = this.activePosition();
    const winSeries = this.visibleIndicatorsWindow();
    if (!nowMs) return [];
    return series.map((s, i) => {
      const sampleInWin = winSeries[i]?.data ?? [];
      const currentPoint = findLastAtOrBefore(s.data, nowMs);
      const entryPoint = active ? findLastAtOrBefore(s.data, active.entryMs) : null;
      const currentValue = currentPoint?.value ?? null;
      const entryValue = entryPoint?.value ?? null;
      const delta = (currentValue !== null && entryValue !== null)
        ? currentValue - entryValue
        : null;
      return {
        name: s.name,
        window: s.window,
        currentValue,
        entryValue,
        delta,
        sparkline: sampleInWin.map(p => p.value as number),
      };
    });
  });

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.clearInterval();
      this.clearFlash();
    });
  }

  load(payload: {
    bars: StockAggregate[];
    trades: BacktestTrade[];
    indicators: IndicatorSeries[];
  }): void {
    this.clearInterval();
    this.clearFlash();
    const sortedBars = [...payload.bars].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    const numberedTrades: TradeWithNumber[] = [...payload.trades]
      .sort((a, b) => new Date(a.entryTimestamp).getTime() - new Date(b.entryTimestamp).getTime())
      .map((t, i) => ({
        ...t,
        tradeNumber: i + 1,
        entryMs: new Date(t.entryTimestamp).getTime(),
        exitMs: new Date(t.exitTimestamp).getTime(),
      }));
    this._bars.set(sortedBars);
    this._trades.set(numberedTrades);
    this._indicators.set(payload.indicators);
    this._currentIndex.set(0);
    this._playbackState.set('stopped');
    this._flashEvent.set(null);
  }

  play(): void {
    if (this.totalBars() === 0) return;
    if (this._direction() === 'forward' && this.isAtEnd()) return;
    if (this._direction() === 'reverse' && this.isAtStart()) return;
    this._playbackState.set('playing');
    this.startInterval();
  }

  pause(): void {
    if (this._playbackState() !== 'playing') return;
    this._playbackState.set('paused');
    this.clearInterval();
  }

  stop(): void {
    this.clearInterval();
    this._currentIndex.set(0);
    this._playbackState.set('stopped');
  }

  stepForward(): void {
    const total = this.totalBars();
    if (total === 0) return;
    const prev = this._currentIndex();
    if (prev >= total - 1) return;
    const next = prev + 1;
    this._currentIndex.set(next);
    this.detectCrossing(prev, next);
    if (next >= total - 1 && this._playbackState() === 'playing') this.pause();
  }

  stepBackward(): void {
    const prev = this._currentIndex();
    if (prev === 0) return;
    const next = prev - 1;
    this._currentIndex.set(next);
    this.detectCrossing(prev, next);
    if (next === 0 && this._playbackState() === 'playing') this.pause();
  }

  seekTo(index: number): void {
    const total = this.totalBars();
    if (total === 0) return;
    const clamped = Math.max(0, Math.min(index, total - 1));
    this._currentIndex.set(clamped);
    this.clearFlash();
  }

  seekToPercent(pct: number): void {
    const total = this.totalBars();
    if (total <= 1) return;
    const clampedPct = Math.max(0, Math.min(1, pct));
    this.seekTo(Math.round(clampedPct * (total - 1)));
  }

  setDirection(dir: Direction): void {
    this._direction.set(dir);
    if (this._playbackState() === 'playing') {
      this.clearInterval();
      this.startInterval();
    }
  }

  toggleDirection(): void {
    this.setDirection(this._direction() === 'forward' ? 'reverse' : 'forward');
  }

  setSpeed(mult: number): void {
    if (mult <= 0) return;
    this._playbackSpeed.set(mult);
    if (this._playbackState() === 'playing') {
      this.clearInterval();
      this.startInterval();
    }
  }

  setWindowSize(size: WindowSize): void {
    this._windowSize.set(size);
  }

  reset(): void {
    this.clearInterval();
    this.clearFlash();
    this._bars.set([]);
    this._trades.set([]);
    this._indicators.set([]);
    this._currentIndex.set(0);
    this._playbackState.set('stopped');
    this._playbackSpeed.set(1);
    this._direction.set('forward');
    this._flashEvent.set(null);
  }

  private detectCrossing(prevIdx: number, nextIdx: number): void {
    const bars = this._bars();
    const prevBar = bars[prevIdx];
    const nextBar = bars[nextIdx];
    if (!prevBar || !nextBar) return;
    const prevMs = new Date(prevBar.timestamp).getTime();
    const nextMs = new Date(nextBar.timestamp).getTime();
    const lo = Math.min(prevMs, nextMs);
    const hi = Math.max(prevMs, nextMs);
    const forward = nextIdx > prevIdx;

    for (const t of this._trades()) {
      // Exit crossing in either direction
      if (t.exitMs > lo && t.exitMs <= hi) {
        this.fireFlash({
          kind: forward ? 'exit' : 'unwind',
          trade: t,
          timestamp: Date.now(),
        });
        return;
      }
    }
  }

  private fireFlash(ev: FlashEvent): void {
    this.clearFlash();
    this._flashEvent.set(ev);
    this.flashTimeoutId = setTimeout(() => {
      this._flashEvent.set(null);
      this.flashTimeoutId = null;
    }, FLASH_DURATION_MS);
  }

  private clearFlash(): void {
    if (this.flashTimeoutId !== null) {
      clearTimeout(this.flashTimeoutId);
      this.flashTimeoutId = null;
    }
  }

  private startInterval(): void {
    this.clearInterval();
    const ms = BASE_INTERVAL_MS / this._playbackSpeed();
    const stepper = this._direction() === 'forward'
      ? () => this.stepForward()
      : () => this.stepBackward();
    this.intervalId = setInterval(stepper, ms);
  }

  private clearInterval(): void {
    if (this.intervalId !== null) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
  }
}

function findLastAtOrBefore(data: IndicatorPoint[], ts: number): IndicatorPoint | null {
  // Assumes data is sorted ascending by timestamp.
  let lo = 0, hi = data.length - 1, result: IndicatorPoint | null = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (data[mid].timestamp <= ts) {
      if (data[mid].value !== null) result = data[mid];
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return result;
}
