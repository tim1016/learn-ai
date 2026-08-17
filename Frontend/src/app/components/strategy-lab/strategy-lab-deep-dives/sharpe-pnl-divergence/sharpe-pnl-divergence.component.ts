import { CurrencyPipe, DecimalPipe, PercentPipe } from '@angular/common';
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
  viewChild,
} from '@angular/core';
import {
  HistogramSeries,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  LineSeries,
  type LineData,
  type UTCTimestamp,
} from 'lightweight-charts';

import type { SharpePnlDivergence } from '../../../lean-engine/engine-results/engine-validation-analytics.types';
import { createAppChart } from '../../../../shared/charts/chart-utils';

export const SHARPE_PNL_DIVERGENCE_CHART_FACTORY = new InjectionToken<typeof createAppChart>(
  'SHARPE_PNL_DIVERGENCE_CHART_FACTORY',
  { providedIn: 'root', factory: () => createAppChart },
);

@Component({
  selector: 'app-sharpe-pnl-divergence',
  imports: [CurrencyPipe, DecimalPipe, PercentPipe],
  templateUrl: './sharpe-pnl-divergence.component.html',
  styleUrl: './sharpe-pnl-divergence.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SharpePnlDivergenceComponent implements AfterViewInit, OnDestroy {
  readonly study = input.required<SharpePnlDivergence>();

  readonly statusLabel = computed(() => {
    const current = this.study().currently_diverging;
    if (current === null) return 'Needs more history';
    return current ? 'Diverging now' : 'No divergence now';
  });
  readonly chartAriaLabel = computed(() =>
    `Cumulative P&L and ${this.study().return_window_sessions}-session rolling Sharpe. ${this.statusLabel()}.`,
  );

  private readonly injector = inject(Injector);
  private readonly createChart = inject(SHARPE_PNL_DIVERGENCE_CHART_FACTORY);
  private readonly chartElement = viewChild<ElementRef<HTMLDivElement>>('chartElement');
  private chart: IChartApi | null = null;
  private pnlSeries: ISeriesApi<'Line'> | null = null;
  private sharpeSeries: ISeriesApi<'Line'> | null = null;
  private divergenceSeries: ISeriesApi<'Histogram'> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private mountedElement: HTMLDivElement | null = null;

  ngAfterViewInit(): void {
    effect(
      () => {
        const element = this.chartElement()?.nativeElement;
        const study = this.study();
        if (element === undefined || study.points.length === 0 || study.error !== null) {
          this.destroyChart();
          return;
        }
        this.ensureChart(element);
        this.renderStudy(study);
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
    const theme = resolveChartTheme(element);

    this.chart = this.createChart(element, {
      width: element.clientWidth,
      height: 340,
      layout: { background: { color: theme.background }, textColor: theme.text },
      grid: {
        vertLines: { color: theme.grid },
        horzLines: { color: theme.grid },
      },
      leftPriceScale: { visible: true, borderColor: theme.border },
      rightPriceScale: { visible: true, borderColor: theme.border },
      timeScale: { borderColor: theme.border, timeVisible: false },
      crosshair: {
        mode: 0,
        vertLine: { color: theme.crosshair, labelBackgroundColor: theme.labelBackground },
        horzLine: { color: theme.crosshair, labelBackgroundColor: theme.labelBackground },
      },
    });
    this.mountedElement = element;
    this.pnlSeries = this.chart.addSeries(LineSeries, {
      title: 'Cumulative P&L',
      priceScaleId: 'left',
      color: theme.pnl,
      lineWidth: 2,
      priceLineVisible: false,
      priceFormat: {
        type: 'custom',
        formatter: (value: number) =>
          value.toLocaleString('en-US', {
            style: 'currency',
            currency: 'USD',
            maximumFractionDigits: 0,
          }),
      },
    });
    this.sharpeSeries = this.chart.addSeries(LineSeries, {
      title: 'Rolling Sharpe',
      priceScaleId: 'right',
      color: theme.sharpe,
      lineWidth: 2,
      priceLineVisible: false,
      priceFormat: {
        type: 'custom',
        formatter: (value: number) => value.toFixed(2),
      },
    });
    this.divergenceSeries = this.chart.addSeries(HistogramSeries, {
      title: 'Divergence',
      priceScaleId: 'divergence',
      color: theme.divergence,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    this.chart.priceScale('divergence').applyOptions({
      visible: false,
      scaleMargins: { top: 0.92, bottom: 0 },
    });

    if (typeof ResizeObserver === 'undefined') return;
    this.resizeObserver = new ResizeObserver(([entry]) => {
      this.chart?.applyOptions({ width: entry.contentRect.width });
    });
    this.resizeObserver.observe(element);
  }

  private renderStudy(study: SharpePnlDivergence): void {
    const pnl: LineData[] = [];
    const sharpe: LineData[] = [];
    const divergence: HistogramData[] = [];
    for (const point of study.points) {
      const time = Math.floor(point.timestamp_ms_utc / 1_000) as UTCTimestamp;
      pnl.push({ time, value: point.cumulative_pnl });
      if (point.rolling_sharpe !== null) {
        sharpe.push({ time, value: point.rolling_sharpe });
      }
      if (point.is_trend_eligible) {
        divergence.push({ time, value: point.is_divergence ? 1 : 0 });
      }
    }
    this.pnlSeries?.setData(pnl);
    this.sharpeSeries?.setData(sharpe);
    this.divergenceSeries?.setData(divergence);
    this.chart?.timeScale().fitContent();
  }

  private destroyChart(): void {
    this.resizeObserver?.disconnect();
    this.chart?.remove();
    this.resizeObserver = null;
    this.chart = null;
    this.pnlSeries = null;
    this.sharpeSeries = null;
    this.divergenceSeries = null;
    this.mountedElement = null;
  }
}

function resolveChartTheme(element: HTMLElement) {
  return {
    background: themeColor(element, '--bg-surface'),
    text: themeColor(element, '--text-subtle'),
    grid: themeColor(element, '--border'),
    border: themeColor(element, '--border-light'),
    crosshair: themeColor(element, '--text-secondary'),
    labelBackground: themeColor(element, '--bg-elevated'),
    pnl: themeColor(element, '--accent-text'),
    sharpe: themeColor(element, '--warn'),
    divergence: themeColor(element, '--bear-soft'),
  };
}

function themeColor(element: HTMLElement, token: string): string {
  const color = getComputedStyle(element).getPropertyValue(token).trim();
  if (color.length === 0) throw new Error(`Required theme token ${token} is not defined`);
  return color;
}
