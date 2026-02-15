import {
  Component, Input, ElementRef, ViewChild,
  AfterViewInit, OnChanges, OnDestroy, SimpleChanges,
  ChangeDetectionStrategy
} from '@angular/core';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, CandlestickData, UTCTimestamp
} from 'lightweight-charts';
import { StockAggregate } from '../../../graphql/types';
import { formatTickMark } from '../chart-utils';

@Component({
  selector: 'app-candlestick-chart',
  standalone: true,
  template: `<div #chartContainer class="chart-container"></div>`,
  styles: [`.chart-container { width: 100%; height: 400px; }`,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class CandlestickChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() data: StockAggregate[] = [];
  @Input() ticker = '';
  @Input() timeVisible = false;
  @ViewChild('chartContainer') chartContainer!: ElementRef<HTMLDivElement>;

  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Candlestick'> | null = null;
  private resizeObserver: ResizeObserver | null = null;

  ngAfterViewInit(): void {
    this.createChart();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (this.chart) {
      if (changes['timeVisible']) {
        this.chart.timeScale().applyOptions({ timeVisible: this.timeVisible });
      }
      if (changes['data']) {
        this.updateData();
      }
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
      timeScale: {
        timeVisible: this.timeVisible,
        secondsVisible: false,
        borderColor: '#ddd',
        minBarSpacing: 0.1,
        tickMarkFormatter: formatTickMark,
      },
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

    const candlestickData: CandlestickData[] = this.data
      .map(agg => ({
        time: (new Date(agg.timestamp).getTime() / 1000) as UTCTimestamp,
        open: agg.open,
        high: agg.high,
        low: agg.low,
        close: agg.close
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    this.series.setData(candlestickData);
    this.chart?.timeScale().fitContent();
  }
}
