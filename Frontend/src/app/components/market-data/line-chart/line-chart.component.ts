import {
  Component, Input, ElementRef, ViewChild,
  AfterViewInit, OnChanges, OnDestroy, SimpleChanges
} from '@angular/core';
import {
  createChart, IChartApi, ISeriesApi,
  LineSeries, LineData, UTCTimestamp
} from 'lightweight-charts';
import { StockAggregate } from '../../../graphql/types';

@Component({
  selector: 'app-line-chart',
  standalone: true,
  template: `<div #chartContainer class="chart-container"></div>`,
  styles: [`.chart-container { width: 100%; height: 300px; }`]
})
export class LineChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() data: StockAggregate[] = [];
  @Input() ticker = '';
  @ViewChild('chartContainer') chartContainer!: ElementRef<HTMLDivElement>;

  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Line'> | null = null;
  private resizeObserver: ResizeObserver | null = null;

  ngAfterViewInit(): void {
    this.createChart();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['data'] && this.chart) {
      this.updateData();
    }
  }

  ngOnDestroy(): void {
    this.resizeObserver?.disconnect();
    this.chart?.remove();
  }

  private createChart(): void {
    const container = this.chartContainer.nativeElement;
    this.chart = createChart(container, {
      width: container.clientWidth,
      height: 300,
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' }
      },
      timeScale: { timeVisible: false, borderColor: '#ddd' }
    });

    this.series = this.chart.addSeries(LineSeries, {
      color: '#2196F3',
      lineWidth: 2,
      crosshairMarkerVisible: true
    });

    this.updateData();

    this.resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0) {
        this.chart?.applyOptions({ width: entries[0].contentRect.width });
      }
    });
    this.resizeObserver.observe(container);
  }

  private updateData(): void {
    if (!this.series || !this.data.length) return;

    const lineData: LineData[] = this.data.map(agg => ({
      time: (new Date(agg.timestamp).getTime() / 1000) as UTCTimestamp,
      value: agg.close
    }));

    this.series.setData(lineData);
    this.chart?.timeScale().fitContent();
  }
}
