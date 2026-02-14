import {
  Component, Input, ElementRef, ViewChild,
  AfterViewInit, OnChanges, OnDestroy, SimpleChanges
} from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, LineSeries,
  CandlestickData, LineData, UTCTimestamp
} from 'lightweight-charts';
import { StockAggregate, IndicatorSeries } from '../../../graphql/types';

@Component({
  selector: 'app-ta-chart',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './ta-chart.component.html',
  styleUrls: ['./ta-chart.component.scss']
})
export class TaChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() aggregates: StockAggregate[] = [];
  @Input() indicators: IndicatorSeries[] = [];
  @Input() ticker = '';

  @ViewChild('priceChartContainer') priceChartContainer!: ElementRef<HTMLDivElement>;
  @ViewChild('rsiChartContainer') rsiChartContainer?: ElementRef<HTMLDivElement>;

  hasRsi = false;
  rsiWindow = 14;

  private priceChart: IChartApi | null = null;
  private rsiChart: IChartApi | null = null;
  private candleSeries: ISeriesApi<'Candlestick'> | null = null;
  private overlaySeries: Map<string, ISeriesApi<'Line'>> = new Map();
  private rsiSeries: ISeriesApi<'Line'> | null = null;
  private resizeObservers: ResizeObserver[] = [];

  private readonly COLORS: Record<string, string> = {
    sma: '#FF6B00',
    ema: '#2196F3',
    bbands: '#9C27B0'
  };

  ngAfterViewInit(): void {
    this.buildCharts();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if ((changes['aggregates'] || changes['indicators']) && this.priceChart) {
      this.updateAll();
    }
  }

  ngOnDestroy(): void {
    this.resizeObservers.forEach(o => o.disconnect());
    this.priceChart?.remove();
    this.rsiChart?.remove();
  }

  private buildCharts(): void {
    const container = this.priceChartContainer.nativeElement;
    this.priceChart = createChart(container, {
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

    this.candleSeries = this.priceChart.addSeries(CandlestickSeries, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderUpColor: '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350'
    });

    this.addResizeObserver(container, this.priceChart);
    this.updateAll();
  }

  private updateAll(): void {
    this.updateCandlesticks();
    this.updateOverlays();
    this.updateRsi();
  }

  private updateCandlesticks(): void {
    if (!this.candleSeries || !this.aggregates.length) return;

    const data: CandlestickData[] = this.aggregates
      .map(a => ({
        time: (new Date(a.timestamp).getTime() / 1000) as UTCTimestamp,
        open: a.open,
        high: a.high,
        low: a.low,
        close: a.close
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    this.candleSeries.setData(data);
    this.priceChart?.timeScale().fitContent();
  }

  private updateOverlays(): void {
    if (!this.priceChart) return;

    // Remove old overlay series
    this.overlaySeries.forEach((series) => {
      this.priceChart!.removeSeries(series);
    });
    this.overlaySeries.clear();

    const overlayIndicators = this.indicators.filter(
      i => i.name === 'sma' || i.name === 'ema' || i.name === 'bbands'
    );

    for (const indicator of overlayIndicators) {
      const color = this.COLORS[indicator.name] || '#999';
      const label = `${indicator.name.toUpperCase()}(${indicator.window})`;

      const series = this.priceChart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        title: label
      });

      const lineData: LineData[] = indicator.data
        .filter(d => d.value !== null)
        .map(d => ({
          time: (d.timestamp / 1000) as UTCTimestamp,
          value: d.value!
        }))
        .sort((a, b) => (a.time as number) - (b.time as number));

      series.setData(lineData);
      this.overlaySeries.set(`${indicator.name}_${indicator.window}`, series);
    }

    this.priceChart.timeScale().fitContent();
  }

  private updateRsi(): void {
    const rsiIndicator = this.indicators.find(i => i.name === 'rsi');
    this.hasRsi = !!rsiIndicator;

    if (!rsiIndicator) {
      if (this.rsiChart) {
        this.rsiChart.remove();
        this.rsiChart = null;
        this.rsiSeries = null;
      }
      return;
    }

    this.rsiWindow = rsiIndicator.window;

    // Build RSI chart lazily after Angular renders the container
    setTimeout(() => {
      if (!this.rsiChartContainer) return;

      if (!this.rsiChart) {
        const container = this.rsiChartContainer.nativeElement;
        this.rsiChart = createChart(container, {
          width: container.clientWidth,
          height: 200,
          layout: { background: { color: '#ffffff' }, textColor: '#333' },
          grid: {
            vertLines: { color: '#f0f0f0' },
            horzLines: { color: '#f0f0f0' }
          },
          timeScale: { timeVisible: false, borderColor: '#ddd' }
        });
        this.addResizeObserver(container, this.rsiChart);
      }

      if (this.rsiSeries) {
        this.rsiChart.removeSeries(this.rsiSeries);
      }

      this.rsiSeries = this.rsiChart.addSeries(LineSeries, {
        color: '#7B1FA2',
        lineWidth: 2,
        title: `RSI(${rsiIndicator.window})`
      });

      const rsiData: LineData[] = rsiIndicator.data
        .filter(d => d.value !== null)
        .map(d => ({
          time: (d.timestamp / 1000) as UTCTimestamp,
          value: d.value!
        }))
        .sort((a, b) => (a.time as number) - (b.time as number));

      this.rsiSeries.setData(rsiData);
      this.rsiChart.timeScale().fitContent();
    }, 0);
  }

  private addResizeObserver(container: HTMLElement, chart: IChartApi): void {
    const observer = new ResizeObserver(entries => {
      if (entries.length > 0) {
        chart.applyOptions({ width: entries[0].contentRect.width });
      }
    });
    observer.observe(container);
    this.resizeObservers.push(observer);
  }
}
