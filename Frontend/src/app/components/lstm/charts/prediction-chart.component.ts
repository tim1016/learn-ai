import {
  Component,
  input,
  effect,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  AfterViewInit,
  OnDestroy,
} from '@angular/core';
import {
  createChart,
  LineSeries,
  IChartApi,
  ISeriesApi,
  LineData,
  UTCTimestamp,
} from 'lightweight-charts';

@Component({
  selector: 'app-prediction-chart',
  standalone: true,
  template: `<div #chartContainer class="chart-container"></div>`,
  styles: [
    `
      .chart-container {
        width: 100%;
        height: 350px;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PredictionChartComponent implements AfterViewInit, OnDestroy {
  actual = input<number[]>([]);
  predicted = input<number[]>([]);

  chartContainer = viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');

  private chart: IChartApi | null = null;
  private actualSeries: ISeriesApi<'Line'> | null = null;
  private predictedSeries: ISeriesApi<'Line'> | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    effect(() => {
      const actual = this.actual();
      const predicted = this.predicted();
      if (this.chart) {
        this.updateData(actual, predicted);
      }
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
      height: 350,
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' },
      },
      rightPriceScale: { borderColor: '#ddd' },
      timeScale: { borderColor: '#ddd' },
    });

    this.actualSeries = this.chart.addSeries(LineSeries, {
      color: '#2196F3',
      lineWidth: 2,
      title: 'Actual',
    });

    this.predictedSeries = this.chart.addSeries(LineSeries, {
      color: '#FF9800',
      lineWidth: 2,
      title: 'Predicted',
      lineStyle: 2,
    });

    this.updateData(this.actual(), this.predicted());

    this.resizeObserver = new ResizeObserver((entries) => {
      if (entries.length > 0) {
        this.chart?.applyOptions({ width: entries[0].contentRect.width });
      }
    });
    this.resizeObserver.observe(container);
  }

  private updateData(actual: number[], predicted: number[]): void {
    if (!this.actualSeries || !this.predictedSeries) return;
    if (!actual.length && !predicted.length) return;

    const toLineData = (values: number[]): LineData[] =>
      values.map((v, i) => ({
        time: (i + 1) as UTCTimestamp,
        value: v,
      }));

    this.actualSeries.setData(toLineData(actual));
    this.predictedSeries.setData(toLineData(predicted));
    this.chart?.timeScale().fitContent();
  }
}
