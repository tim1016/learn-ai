import {
  Component, Input, ElementRef, ViewChild,
  AfterViewInit, OnChanges, OnDestroy, SimpleChanges,
  ChangeDetectionStrategy,
} from '@angular/core';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, CandlestickData, LineSeries, LineData,
  UTCTimestamp, Time, SeriesMarker, createSeriesMarkers,
  ISeriesMarkersPluginApi,
} from 'lightweight-charts';
import { StockAggregate, BacktestTrade } from '../../../graphql/types';
import { VisibleIndicatorSeries } from '../../../services/replay-indicator.service';
import { formatTickMark } from '../../market-data/chart-utils';

const INDICATOR_COLORS = ['#ff9800', '#2196f3', '#9c27b0', '#4caf50', '#f44336'];

@Component({
  selector: 'app-replay-chart',
  standalone: true,
  template: `<div #chartContainer class="chart-container"></div>`,
  styles: [`.chart-container { width: 100%; height: 450px; }`],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReplayChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() data: StockAggregate[] = [];
  @Input() ticker = '';
  @Input() indicators: VisibleIndicatorSeries[] = [];
  @Input() visibleTrades: BacktestTrade[] = [];
  @Input() activePosition: BacktestTrade | null = null;
  @ViewChild('chartContainer') chartContainer!: ElementRef<HTMLDivElement>;

  private chart: IChartApi | null = null;
  private candlestickSeries: ISeriesApi<'Candlestick'> | null = null;
  private indicatorLineSeries: ISeriesApi<'Line'>[] = [];
  private markersPlugin: ISeriesMarkersPluginApi<Time> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private previousDataLength = 0;

  ngAfterViewInit(): void {
    this.createChart();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (!this.chart) return;

    if (changes['data']) {
      this.updateCandlestickData();
    }
    if (changes['indicators']) {
      this.updateIndicatorOverlays();
    }
    if (changes['visibleTrades'] || changes['activePosition']) {
      this.updateTradeMarkers();
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
      height: 450,
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: '#ddd',
        minBarSpacing: 0.1,
        tickMarkFormatter: formatTickMark,
      },
      crosshair: { mode: 0 },
    });

    this.candlestickSeries = this.chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderUpColor: '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    });

    this.markersPlugin = createSeriesMarkers(this.candlestickSeries, []);

    this.updateCandlestickData();
    this.updateIndicatorOverlays();
    this.updateTradeMarkers();

    this.resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0) {
        const { width } = entries[0].contentRect;
        this.chart?.applyOptions({ width });
      }
    });
    this.resizeObserver.observe(container);
  }

  private updateCandlestickData(): void {
    if (!this.candlestickSeries || !this.data.length) return;

    const isSequentialAdvance = this.data.length === this.previousDataLength + 1;
    this.previousDataLength = this.data.length;

    const candlestickData: CandlestickData[] = this.data
      .map(agg => ({
        time: (new Date(agg.timestamp).getTime() / 1000) as UTCTimestamp,
        open: agg.open,
        high: agg.high,
        low: agg.low,
        close: agg.close,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    if (isSequentialAdvance && candlestickData.length > 0) {
      // O(1) update: just add the last bar
      this.candlestickSeries.update(candlestickData[candlestickData.length - 1]);
    } else {
      // Full rebuild: used for seek operations
      this.candlestickSeries.setData(candlestickData);
      this.chart?.timeScale().fitContent();
    }
  }

  private updateIndicatorOverlays(): void {
    if (!this.chart) return;

    // Remove stale series
    for (const series of this.indicatorLineSeries) {
      this.chart.removeSeries(series);
    }
    this.indicatorLineSeries = [];

    // Create new series for each indicator
    for (let i = 0; i < this.indicators.length; i++) {
      const indicator = this.indicators[i];
      const color = INDICATOR_COLORS[i % INDICATOR_COLORS.length];

      const lineSeries = this.chart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        title: `${indicator.name.toUpperCase()}(${indicator.window})`,
        priceLineVisible: false,
        lastValueVisible: false,
      });

      const lineData: LineData[] = indicator.data
        .filter(p => p.value !== null)
        .map(p => ({
          time: (p.timestamp / 1000) as UTCTimestamp,
          value: p.value!,
        }))
        .sort((a, b) => (a.time as number) - (b.time as number));

      lineSeries.setData(lineData);
      this.indicatorLineSeries.push(lineSeries);
    }
  }

  private updateTradeMarkers(): void {
    if (!this.markersPlugin) return;

    const markers: SeriesMarker<Time>[] = [];

    for (const trade of this.visibleTrades) {
      // Entry marker
      markers.push({
        time: (new Date(trade.entryTimestamp).getTime() / 1000) as UTCTimestamp,
        position: 'belowBar',
        color: '#2196f3',
        shape: 'arrowUp',
        text: `${trade.tradeType} Entry`,
      });

      // Exit marker (only if the trade is completed — exit timestamp visible)
      const exitTimeMs = new Date(trade.exitTimestamp).getTime();
      const currentBarTimeMs = this.data.length > 0
        ? new Date(this.data[this.data.length - 1].timestamp).getTime()
        : 0;

      if (exitTimeMs <= currentBarTimeMs) {
        markers.push({
          time: (exitTimeMs / 1000) as UTCTimestamp,
          position: 'aboveBar',
          color: trade.pnl >= 0 ? '#4caf50' : '#f44336',
          shape: 'arrowDown',
          text: `Exit ${trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(2)}`,
        });
      }
    }

    // Active position highlight
    if (this.activePosition && this.data.length > 0) {
      const lastBar = this.data[this.data.length - 1];
      markers.push({
        time: (new Date(lastBar.timestamp).getTime() / 1000) as UTCTimestamp,
        position: 'aboveBar',
        color: '#ff9800',
        shape: 'circle',
        text: 'OPEN',
      });
    }

    // Sort markers by time (required by lightweight-charts)
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    this.markersPlugin.setMarkers(markers);
  }
}
