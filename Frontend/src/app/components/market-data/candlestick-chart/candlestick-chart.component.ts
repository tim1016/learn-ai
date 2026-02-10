import {
  Component, Input, ElementRef, ViewChild,
  AfterViewInit, OnChanges, OnDestroy, SimpleChanges
} from '@angular/core';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, CandlestickData, UTCTimestamp
} from 'lightweight-charts';
import { StockAggregate } from '../../../graphql/types';

@Component({
  selector: 'app-candlestick-chart',
  standalone: true,
  template: `<div #chartContainer class="chart-container"></div>`,
  styles: [`.chart-container { width: 100%; height: 400px; }`]
})
export class CandlestickChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() data: StockAggregate[] = [];
  @Input() ticker = '';
  @ViewChild('chartContainer') chartContainer!: ElementRef<HTMLDivElement>;

  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Candlestick'> | null = null;
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
      height: 400,
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' }
      },
      timeScale: { timeVisible: false, borderColor: '#ddd' },
      crosshair: { mode: 0 }
    });

    this.series = this.chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderUpColor: '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350'
    });

    this.updateData();

    this.resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0) {
        const { width } = entries[0].contentRect;
        this.chart?.applyOptions({ width });
      }
    });
    this.resizeObserver.observe(container);
  }

  private updateData(): void {
    if (!this.series || !this.data.length) return;

    const candlestickData: CandlestickData[] = this.data.map(agg => ({
      time: (new Date(agg.timestamp).getTime() / 1000) as UTCTimestamp,
      open: agg.open,
      high: agg.high,
      low: agg.low,
      close: agg.close
    }));

    this.series.setData(candlestickData);
    this.chart?.timeScale().fitContent();
  }
}
