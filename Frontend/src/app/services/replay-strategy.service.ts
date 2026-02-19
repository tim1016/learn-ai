import { Injectable, signal, computed, inject } from '@angular/core';
import { BacktestTrade } from '../graphql/types';
import { ReplayEngineService } from './replay-engine.service';

@Injectable({ providedIn: 'root' })
export class ReplayStrategyService {
  private readonly replayEngine = inject(ReplayEngineService);

  private readonly _allTrades = signal<BacktestTrade[]>([]);

  readonly allTrades = this._allTrades.asReadonly();

  readonly visibleTrades = computed<BacktestTrade[]>(() => {
    const trades = this._allTrades();
    const currentBar = this.replayEngine.currentBar();
    if (!currentBar || trades.length === 0) return [];

    const currentTimestampMs = new Date(currentBar.timestamp).getTime();

    return trades.filter(
      t => new Date(t.entryTimestamp).getTime() <= currentTimestampMs
    );
  });

  readonly completedTrades = computed<BacktestTrade[]>(() => {
    const trades = this._allTrades();
    const currentBar = this.replayEngine.currentBar();
    if (!currentBar || trades.length === 0) return [];

    const currentTimestampMs = new Date(currentBar.timestamp).getTime();

    return trades.filter(
      t => new Date(t.exitTimestamp).getTime() <= currentTimestampMs
    );
  });

  readonly activePosition = computed<BacktestTrade | null>(() => {
    const trades = this._allTrades();
    const currentBar = this.replayEngine.currentBar();
    if (!currentBar || trades.length === 0) return null;

    const currentTimestampMs = new Date(currentBar.timestamp).getTime();

    return trades.find(
      t =>
        new Date(t.entryTimestamp).getTime() <= currentTimestampMs &&
        new Date(t.exitTimestamp).getTime() > currentTimestampMs
    ) ?? null;
  });

  loadTrades(trades: BacktestTrade[]): void {
    this._allTrades.set(trades);
  }

  reset(): void {
    this._allTrades.set([]);
  }
}
