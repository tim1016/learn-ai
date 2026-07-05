import {
  Component, signal, computed, input,
  viewChild, ElementRef, AfterViewInit, OnDestroy,
  ChangeDetectionStrategy, effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, CandlestickData,
  HistogramSeries, HistogramData,
  AreaSeries, AreaData,
  UTCTimestamp,
  createSeriesMarkers, ISeriesMarkersPluginApi,
} from 'lightweight-charts';

// ──────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────
export interface ChartBar {
  t: number;   // ms timestamp
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface EngineTradeForChart {
  entry_time: number;
  exit_time: number;
  entry_price: number;
  exit_price: number;
  pnl_pts: number;
  result: string;
}

export interface EquityCurvePoint {
  timestamp: number;
  equity: number;
}

// ──────────────────────────────────────────────
// Dark theme palette — aligned to _tokens.scss so the chart panel
// reads as the same surface as the rest of the app's cards.
// (Pre-refactor hex like #0f1117 / #00c896 drifted from the current
// TV-dark tokens and made the chart look like a different shade
// of dark than its neighbours.)
// ──────────────────────────────────────────────
const DARK = {
  bg:         '#131722', // $bg-surface
  surface:    '#1b1f2e', // $bg-elevated
  grid:       'rgba(42, 46, 57, 0.5)', // $border-light @ 50%
  text:       '#9598a1', // $text-subtle (5.2:1 AA)
  border:     '#2a2e39', // $border-light
  crosshair:  '#4a5068',
  bull:       '#26a69a', // $bull (TV green)
  bear:       '#ef5350', // $bear (TV red)
  bullVolume: 'rgba(38, 166, 154, 0.20)',
  bearVolume: 'rgba(239, 83, 80, 0.20)',
};

@Component({
  selector: 'app-engine-chart',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './engine-chart.component.html',
  styleUrls: ['./engine-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EngineChartComponent implements AfterViewInit, OnDestroy {
  // Inputs
  chartBars = input<ChartBar[]>([]);
  trades = input<EngineTradeForChart[]>([]);
  equityCurve = input<EquityCurvePoint[]>([]);

  // Template refs
  priceChartEl = viewChild<ElementRef<HTMLDivElement>>('priceChart');
  equityChartEl = viewChild<ElementRef<HTMLDivElement>>('equityChart');

  // State
  activeView = signal<'price' | 'equity'>('price');
  hasData = computed(() => this.chartBars().length > 0);
  hasEquity = computed(() => this.equityCurve().length > 0);

  // Chart instances
  private priceChart: IChartApi | null = null;
  private candleSeries: ISeriesApi<'Candlestick'> | null = null;
  private volumeSeries: ISeriesApi<'Histogram'> | null = null;
  private markersPlugin: ISeriesMarkersPluginApi<any> | null = null;
  private equityChart: IChartApi | null = null;
  private equitySeries: ISeriesApi<'Area'> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private initialized = false;

  constructor() {
    effect(() => {
      const bars = this.chartBars();
      const trades = this.trades();
      const equity = this.equityCurve();
      if (this.initialized && (bars.length > 0 || equity.length > 0)) {
        setTimeout(() => this.renderAll());
      }
    });
  }

  ngAfterViewInit(): void {
    this.createPriceChart();
    this.createEquityChart();
    this.initialized = true;
    if (this.chartBars().length > 0 || this.equityCurve().length > 0) {
      setTimeout(() => this.renderAll());
    }
  }

  ngOnDestroy(): void {
    this.resizeObserver?.disconnect();
    this.markersPlugin?.detach();
    this.priceChart?.remove();
    this.equityChart?.remove();
  }

  setView(view: 'price' | 'equity'): void {
    this.activeView.set(view);
    // Charts need a resize after becoming visible
    setTimeout(() => {
      const priceEl = this.priceChartEl()?.nativeElement;
      const equityEl = this.equityChartEl()?.nativeElement;
      if (view === 'price' && priceEl) {
        this.priceChart?.applyOptions({ width: priceEl.clientWidth });
      }
      if (view === 'equity' && equityEl) {
        this.equityChart?.applyOptions({ width: equityEl.clientWidth });
      }
    });
  }

  // ──────────────────────────────────────────────
  // Price chart (candlestick + volume + trade markers)
  // ──────────────────────────────────────────────
  private createPriceChart(): void {
    const el = this.priceChartEl()?.nativeElement;
    if (!el) return;

    this.priceChart = createChart(el, {
      width: el.clientWidth,
      height: 480,
      layout: { background: { color: DARK.bg }, textColor: DARK.text },
      grid: {
        vertLines: { color: DARK.grid },
        horzLines: { color: DARK.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: DARK.border,
        minBarSpacing: 0.5,
      },
      crosshair: {
        mode: 0,
        vertLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
        horzLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
      },
      rightPriceScale: { borderColor: DARK.border },
    });

    this.candleSeries = this.priceChart.addSeries(CandlestickSeries, {
      upColor: DARK.bull,
      downColor: DARK.bear,
      borderUpColor: DARK.bull,
      borderDownColor: DARK.bear,
      wickUpColor: DARK.bull,
      wickDownColor: DARK.bear,
    });

    this.volumeSeries = this.priceChart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });

    this.priceChart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    this.resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0) {
        const { width } = entries[0].contentRect;
        this.priceChart?.applyOptions({ width });
        this.equityChart?.applyOptions({ width });
      }
    });
    this.resizeObserver.observe(el);
  }

  // ──────────────────────────────────────────────
  // Equity curve chart
  // ──────────────────────────────────────────────
  private createEquityChart(): void {
    const el = this.equityChartEl()?.nativeElement;
    if (!el) return;

    this.equityChart = createChart(el, {
      width: el.clientWidth,
      height: 300,
      layout: { background: { color: DARK.bg }, textColor: DARK.text },
      grid: {
        vertLines: { color: DARK.grid },
        horzLines: { color: DARK.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: DARK.border,
      },
      crosshair: {
        mode: 0,
        vertLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
        horzLine: { color: DARK.crosshair, labelBackgroundColor: DARK.surface },
      },
      rightPriceScale: {
        borderColor: DARK.border,
      },
    });

    this.equitySeries = this.equityChart.addSeries(AreaSeries, {
      lineColor: '#5b8def',
      topColor: 'rgba(91, 141, 239, 0.3)',
      bottomColor: 'rgba(91, 141, 239, 0.02)',
      lineWidth: 2,
      priceFormat: {
        type: 'custom',
        formatter: (price: number) => '$' + price.toLocaleString('en-US', { maximumFractionDigits: 0 }),
      },
    });
  }

  // ──────────────────────────────────────────────
  // Render all
  // ──────────────────────────────────────────────
  private renderAll(): void {
    this.renderCandles();
    this.renderTradeMarkers();
    this.renderEquityCurve();
  }

  private renderCandles(): void {
    if (!this.candleSeries || !this.volumeSeries) return;
    const bars = this.chartBars();
    if (!bars.length) return;

    const candleData: CandlestickData[] = bars
      .map(bar => ({
        time: (bar.t / 1000) as UTCTimestamp,
        open: bar.o,
        high: bar.h,
        low: bar.l,
        close: bar.c,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    this.candleSeries.setData(candleData);

    const volumeData: HistogramData[] = bars
      .map(bar => ({
        time: (bar.t / 1000) as UTCTimestamp,
        value: bar.v ?? 0,
        color: bar.c >= bar.o ? DARK.bullVolume : DARK.bearVolume,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    this.volumeSeries.setData(volumeData);

    // Fit content to show all bars
    this.priceChart?.timeScale().fitContent();
  }

  private renderTradeMarkers(): void {
    if (!this.candleSeries) return;
    const tradeList = this.trades();
    if (!tradeList.length) return;

    const markers: any[] = [];

    for (const trade of tradeList) {
      const entryTs = Math.floor(trade.entry_time / 1000);
      const exitTs = Math.floor(trade.exit_time / 1000);

      markers.push({
        time: entryTs as UTCTimestamp,
        position: 'belowBar',
        color: '#2196f3',
        shape: 'arrowUp',
        text: 'BUY',
      });

      markers.push({
        time: exitTs as UTCTimestamp,
        position: 'aboveBar',
        color: trade.result === 'WIN' ? '#4caf50' : '#f44336',
        shape: 'arrowDown',
        text: `${trade.pnl_pts >= 0 ? '+' : ''}${trade.pnl_pts.toFixed(2)}`,
      });
    }

    markers.sort((a, b) => (a.time as number) - (b.time as number));

    if (this.markersPlugin) {
      this.markersPlugin.setMarkers(markers);
    } else {
      this.markersPlugin = createSeriesMarkers(this.candleSeries, markers);
    }
  }

  private renderEquityCurve(): void {
    if (!this.equitySeries) return;
    const curve = this.equityCurve();
    if (!curve.length) return;

    // Downsample if too many points (minute bars over 2 years = ~500k points)
    const maxPoints = 2000;
    const step = Math.max(1, Math.floor(curve.length / maxPoints));

    const data: AreaData[] = [];
    for (let i = 0; i < curve.length; i += step) {
      const pt = curve[i];
      const ts = pt.timestamp / 1000;
      if (!isNaN(ts)) {
        data.push({ time: ts as UTCTimestamp, value: pt.equity });
      }
    }
    // Always include last point
    if (step > 1 && curve.length > 0) {
      const last = curve[curve.length - 1];
      const ts = last.timestamp / 1000;
      if (!isNaN(ts)) {
        data.push({ time: ts as UTCTimestamp, value: last.equity });
      }
    }

    data.sort((a, b) => (a.time as number) - (b.time as number));
    this.equitySeries.setData(data);
    this.equityChart?.timeScale().fitContent();
  }

}
