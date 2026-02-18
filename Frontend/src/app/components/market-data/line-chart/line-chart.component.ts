import {
  Component, input, viewChild, effect,
  ElementRef, AfterViewInit, OnDestroy,
  ChangeDetectionStrategy
} from '@angular/core';
import {
  createChart, IChartApi, ISeriesApi,
  LineSeries, LineData, UTCTimestamp
} from 'lightweight-charts';
import { StockAggregate } from '../../../graphql/types';
import { formatTickMark } from '../chart-utils';

@Component({
  selector: 'app-line-chart',
  standalone: true,
  template: `<div #chartContainer class="chart-container" [style.height.px]="height()"></div>`,
  styles: [`.chart-container { width: 100%; }`],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LineChartComponent implements AfterViewInit, OnDestroy {
  data = input<StockAggregate[]>([]);
  ticker = input('');
  timeVisible = input(false);
  height = input(400);
  lineColor = input('#2196F3');
  chartContainer = viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');

  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Line'> | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    effect(() => {
      const data = this.data();
      const timeVisible = this.timeVisible();
      if (!this.chart) return;
      this.chart.timeScale().applyOptions({ timeVisible });
      this.updateData(data);
    });
  }

  ngAfterViewInit(): void {
    this.initChart();
  }

  ngOnDestroy(): void {
    this.resizeObserver?.disconnect();
    this.chart?.remove();
  }

  private initChart(): void {
    const container = this.chartContainer().nativeElement;
    this.chart = createChart(container, {
      width: container.clientWidth,
      height: this.height(),
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' }
      },
      timeScale: {
        timeVisible: this.timeVisible(),
        secondsVisible: false,
        borderColor: '#ddd',
        minBarSpacing: 0.001,
        tickMarkFormatter: formatTickMark,
      },
      crosshair: { mode: 0 }
    });

    this.series = this.chart.addSeries(LineSeries, {
      color: this.lineColor(),
      lineWidth: 2,
      crosshairMarkerVisible: true,
      lastValueVisible: true,
      priceLineVisible: true,
    });

    this.updateData(this.data());

    this.resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0) {
        this.chart?.applyOptions({ width: entries[0].contentRect.width });
      }
    });
    this.resizeObserver.observe(container);
  }

  private updateData(data: StockAggregate[]): void {
    if (!this.series || !data.length) return;

    const lineData: LineData[] = data
      .map(agg => ({
        time: (new Date(agg.timestamp).getTime() / 1000) as UTCTimestamp,
        value: agg.close
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    this.series.setData(lineData);
    this.chart?.timeScale().fitContent();
  }
}
