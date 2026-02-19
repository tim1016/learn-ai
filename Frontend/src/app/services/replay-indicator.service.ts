import { Injectable, signal, computed, inject } from '@angular/core';
import { IndicatorSeries, IndicatorPoint } from '../graphql/types';
import { ReplayEngineService } from './replay-engine.service';

export interface VisibleIndicatorSeries {
  name: string;
  window: number;
  data: IndicatorPoint[];
}

@Injectable({ providedIn: 'root' })
export class ReplayIndicatorService {
  private readonly replayEngine = inject(ReplayEngineService);

  private readonly _indicatorSeries = signal<IndicatorSeries[]>([]);

  readonly indicatorSeries = this._indicatorSeries.asReadonly();

  readonly visibleIndicators = computed<VisibleIndicatorSeries[]>(() => {
    const series = this._indicatorSeries();
    const currentBar = this.replayEngine.currentBar();
    if (!currentBar || series.length === 0) return [];

    const currentTimestampMs = new Date(currentBar.timestamp).getTime();

    return series.map(s => ({
      name: s.name,
      window: s.window,
      data: s.data.filter(point => point.timestamp <= currentTimestampMs),
    }));
  });

  loadIndicators(series: IndicatorSeries[]): void {
    this._indicatorSeries.set(series);
  }

  reset(): void {
    this._indicatorSeries.set([]);
  }
}
