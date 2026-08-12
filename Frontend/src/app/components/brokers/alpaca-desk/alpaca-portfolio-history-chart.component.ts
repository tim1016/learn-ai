import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  effect,
  ElementRef,
  input,
  inject,
  InjectionToken,
  Injector,
  OnDestroy,
  viewChild,
} from '@angular/core';
import {
  IChartApi,
  ISeriesApi,
  LineSeries,
  UTCTimestamp,
} from 'lightweight-charts';

import type { BrokerPortfolioHistory } from '../../../api/alpaca.types';
import { createAppChart } from '../../../shared/charts/chart-utils';

export const ALPACA_PORTFOLIO_HISTORY_CHART_FACTORY = new InjectionToken<typeof createAppChart>(
  'ALPACA_PORTFOLIO_HISTORY_CHART_FACTORY',
  { providedIn: 'root', factory: () => createAppChart },
);

/** Direct renderer for the broker-owned C1 equity curve; it derives no P&L. */
@Component({
  selector: 'app-alpaca-portfolio-history-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './alpaca-portfolio-history-chart.component.html',
  styleUrl: './alpaca-portfolio-history-chart.component.scss',
})
export class AlpacaPortfolioHistoryChartComponent implements AfterViewInit, OnDestroy {
  readonly history = input<BrokerPortfolioHistory | undefined>(undefined);
  readonly scopeLabel = input.required<string>();
  readonly unavailable = input(false);

  private readonly injector = inject(Injector);
  private readonly createChart = inject(ALPACA_PORTFOLIO_HISTORY_CHART_FACTORY);
  private readonly chartContainer = viewChild<ElementRef<HTMLDivElement>>('chartContainer');
  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Line'> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private chartElement: HTMLDivElement | null = null;

  ngAfterViewInit(): void {
    effect(
      () => {
        const history = this.history();
        const container = this.chartContainer();
        if (history === undefined || container === undefined || history.timestamps.length === 0) {
          this.destroyChart();
          return;
        }

        this.ensureChart(container.nativeElement);
        this.series?.setData(
          history.timestamps.map((timestamp, index) => ({
            time: (timestamp / 1_000) as UTCTimestamp,
            value: history.equity[index],
          })),
        );
        this.chart?.timeScale().fitContent();
      },
      { injector: this.injector },
    );
  }

  ngOnDestroy(): void {
    this.destroyChart();
  }

  private destroyChart(): void {
    this.resizeObserver?.disconnect();
    this.chart?.remove();
    this.resizeObserver = null;
    this.series = null;
    this.chart = null;
    this.chartElement = null;
  }

  private ensureChart(container: HTMLDivElement): void {
    if (this.chartElement !== container) this.destroyChart();
    if (this.chart !== null) return;

    this.chart = this.createChart(container, {
      width: container.clientWidth,
      height: 300,
      layout: { background: { color: 'transparent' }, textColor: '#aeb6c9' },
      grid: {
        vertLines: { color: 'rgba(174, 182, 201, 0.12)' },
        horzLines: { color: 'rgba(174, 182, 201, 0.12)' },
      },
      rightPriceScale: { borderColor: 'rgba(174, 182, 201, 0.2)' },
      timeScale: { borderColor: 'rgba(174, 182, 201, 0.2)' },
    });
    this.chartElement = container;
    this.series = this.chart.addSeries(LineSeries, {
      color: '#6ee7b7',
      lineWidth: 2,
      priceLineVisible: false,
    });
    if (typeof ResizeObserver === 'undefined') return;
    this.resizeObserver = new ResizeObserver(([entry]) => {
      this.chart?.applyOptions({ width: entry.contentRect.width });
    });
    this.resizeObserver.observe(container);
  }
}
