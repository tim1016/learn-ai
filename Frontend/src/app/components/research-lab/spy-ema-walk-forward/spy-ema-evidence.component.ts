import { DecimalPipe, PercentPipe } from '@angular/common';
import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  ElementRef,
  inject,
  InjectionToken,
  Injector,
  input,
  OnDestroy,
  output,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import {
  AreaSeries,
  type AreaData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';

import type {
  EquityCurvePoint,
  StrategyRunResponse,
} from '../../../services/strategy-runs.types';
import type { WalkForwardResponse } from '../../../services/walk-forward.types';
import { createAppChart } from '../../../shared/charts/chart-utils';

export const SPY_EMA_EVIDENCE_CHART_FACTORY = new InjectionToken<typeof createAppChart>(
  'SPY_EMA_EVIDENCE_CHART_FACTORY',
  { providedIn: 'root', factory: () => createAppChart },
);

/** Server-evidence panel; maps persisted results to UI without deriving metrics. */
@Component({
  selector: 'app-spy-ema-evidence',
  imports: [DecimalPipe, PercentPipe, RouterLink],
  templateUrl: './spy-ema-evidence.component.html',
  styleUrl: './spy-ema-evidence.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SpyEmaEvidenceComponent implements AfterViewInit, OnDestroy {
  readonly control = input<StrategyRunResponse | null>(null);
  readonly walkForward = input<WalkForwardResponse | null>(null);
  readonly loading = input(false);
  readonly running = input(false);
  readonly error = input<string | null>(null);
  readonly walkForwardError = input<string | null>(null);
  readonly refreshRequested = output();

  readonly curve = computed<readonly EquityCurvePoint[]>(() => {
    const walkForward = this.walkForward();
    return walkForward === null
      ? this.control()?.result.equity_curve ?? []
      : walkForward.result.combined_oos_equity_curve;
  });
  readonly hasParameterSearch = computed(() =>
    this.walkForward()?.config.parameter_search !== null
    && this.walkForward()?.config.parameter_search !== undefined,
  );
  readonly curveLabel = computed(() =>
    this.walkForward() !== null
      ? this.hasParameterSearch()
        ? 'Combined selected-threshold OOS equity'
        : 'Combined fixed-parameter OOS equity'
      : 'Canonical control equity',
  );
  readonly completedFoldCount = computed(() =>
    this.walkForward()?.result.folds.filter(fold => fold.status === 'completed').length ?? 0,
  );
  readonly failedFoldCount = computed(() =>
    this.walkForward()?.result.folds.filter(fold => fold.status === 'failed').length ?? 0,
  );
  readonly curveAriaLabel = computed(() =>
    `${this.curveLabel()} curve from persisted Python research results`,
  );

  private readonly injector = inject(Injector);
  private readonly createChart = inject(SPY_EMA_EVIDENCE_CHART_FACTORY);
  private readonly chartElement = viewChild<ElementRef<HTMLDivElement>>('chartElement');
  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Area'> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private mountedElement: HTMLDivElement | null = null;

  ngAfterViewInit(): void {
    effect(
      () => {
        const element = this.chartElement()?.nativeElement;
        const points = this.curve();
        if (element === undefined || points.length === 0) {
          this.destroyChart();
          return;
        }
        this.ensureChart(element);
        this.series?.setData(toAreaData(points));
        this.chart?.timeScale().fitContent();
      },
      { injector: this.injector },
    );
  }

  ngOnDestroy(): void {
    this.destroyChart();
  }

  private ensureChart(element: HTMLDivElement): void {
    if (this.mountedElement !== element) this.destroyChart();
    if (this.chart !== null) return;

    this.chart = this.createChart(element, {
      width: element.clientWidth,
      height: 280,
      layout: { background: { color: 'transparent' }, textColor: '#9598a1' },
      grid: {
        vertLines: { color: 'rgba(149, 152, 161, 0.1)' },
        horzLines: { color: 'rgba(149, 152, 161, 0.1)' },
      },
      rightPriceScale: { borderColor: '#2a2e39' },
      timeScale: { borderColor: '#2a2e39', timeVisible: true },
    });
    this.mountedElement = element;
    this.series = this.chart.addSeries(AreaSeries, {
      lineColor: '#26a69a',
      topColor: 'rgba(38, 166, 154, 0.3)',
      bottomColor: 'rgba(38, 166, 154, 0.015)',
      lineWidth: 2,
      priceLineVisible: false,
      priceFormat: {
        type: 'custom',
        formatter: (value: number) =>
          `$${value.toLocaleString('en-US', { maximumFractionDigits: 0 })}`,
      },
    });

    if (typeof ResizeObserver === 'undefined') return;
    this.resizeObserver = new ResizeObserver(([entry]) => {
      this.chart?.applyOptions({ width: entry.contentRect.width });
    });
    this.resizeObserver.observe(element);
  }

  private destroyChart(): void {
    this.resizeObserver?.disconnect();
    this.chart?.remove();
    this.resizeObserver = null;
    this.chart = null;
    this.series = null;
    this.mountedElement = null;
  }
}

function toAreaData(points: readonly EquityCurvePoint[]): AreaData[] {
  const byTime = new Map<number, AreaData>();
  for (const point of points) {
    const time = Math.floor(point.timestamp_ms / 1_000) as UTCTimestamp;
    byTime.set(time, { time, value: point.equity });
  }
  return [...byTime.values()].sort((left, right) =>
    Number(left.time) - Number(right.time));
}
