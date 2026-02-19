import {
  Injectable, signal, computed, DestroyRef, inject,
} from '@angular/core';
import { StockAggregate } from '../graphql/types';

export type PlaybackState = 'stopped' | 'playing' | 'paused';

@Injectable({ providedIn: 'root' })
export class ReplayEngineService {
  private readonly destroyRef = inject(DestroyRef);
  private intervalId: ReturnType<typeof setInterval> | null = null;

  private readonly BASE_INTERVAL_MS = 100;

  // Core state
  private readonly _bars = signal<StockAggregate[]>([]);
  private readonly _currentIndex = signal(0);
  private readonly _playbackState = signal<PlaybackState>('stopped');
  private readonly _playbackSpeed = signal(1);

  // Public readonly signals
  readonly bars = this._bars.asReadonly();
  readonly currentIndex = this._currentIndex.asReadonly();
  readonly playbackState = this._playbackState.asReadonly();
  readonly playbackSpeed = this._playbackSpeed.asReadonly();

  // Computed signals
  readonly totalBars = computed(() => this._bars().length);

  readonly visibleBars = computed(() =>
    this._bars().slice(0, this._currentIndex() + 1)
  );

  readonly currentBar = computed(() =>
    this._bars()[this._currentIndex()] ?? null
  );

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

  constructor() {
    this.destroyRef.onDestroy(() => this.clearInterval());
  }

  load(bars: StockAggregate[]): void {
    this.clearInterval();
    const sorted = [...bars].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    this._bars.set(sorted);
    this._currentIndex.set(0);
    this._playbackState.set('stopped');
  }

  play(): void {
    if (this.totalBars() === 0) return;
    if (this.isAtEnd()) return;

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

    const current = this._currentIndex();
    if (current < total - 1) {
      this._currentIndex.set(current + 1);
    }

    if (this._currentIndex() >= total - 1 && this._playbackState() === 'playing') {
      this.pause();
    }
  }

  stepBackward(): void {
    const current = this._currentIndex();
    if (current > 0) {
      this._currentIndex.set(current - 1);
    }
  }

  seekTo(index: number): void {
    const total = this.totalBars();
    if (total === 0) return;

    const clamped = Math.max(0, Math.min(index, total - 1));
    this._currentIndex.set(clamped);
  }

  seekToPercent(pct: number): void {
    const total = this.totalBars();
    if (total <= 1) return;

    const clampedPct = Math.max(0, Math.min(1, pct));
    const index = Math.round(clampedPct * (total - 1));
    this._currentIndex.set(index);
  }

  setSpeed(multiplier: number): void {
    if (multiplier <= 0) return;
    this._playbackSpeed.set(multiplier);

    if (this._playbackState() === 'playing') {
      this.clearInterval();
      this.startInterval();
    }
  }

  reset(): void {
    this.clearInterval();
    this._bars.set([]);
    this._currentIndex.set(0);
    this._playbackState.set('stopped');
    this._playbackSpeed.set(1);
  }

  private startInterval(): void {
    this.clearInterval();
    const ms = this.BASE_INTERVAL_MS / this._playbackSpeed();
    this.intervalId = setInterval(() => this.stepForward(), ms);
  }

  private clearInterval(): void {
    if (this.intervalId !== null) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
  }
}
