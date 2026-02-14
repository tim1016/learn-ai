import {
  Component, ElementRef, DestroyRef, ChangeDetectionStrategy,
  input, computed, effect, signal, viewChild, inject, afterNextRender,
} from '@angular/core';
import {
  createChart, IChartApi, ISeriesApi,
  CandlestickSeries, LineSeries,
  CandlestickData, LineData, UTCTimestamp,
} from 'lightweight-charts';
import { StockAggregate, IndicatorSeries } from '../../../graphql/types';

const INDICATOR_COLORS: Record<string, string> = {
  sma: '#FF6B00',
  ema: '#2196F3',
  bbands: '#9C27B0',
  rsi: '#7B1FA2',
};

const OVERLAY_NAMES = new Set(['sma', 'ema', 'bbands']);

@Component({
  selector: 'app-ta-chart',
  standalone: true,
  templateUrl: './ta-chart.component.html',
  styleUrls: ['./ta-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TaChartComponent {
  private destroyRef = inject(DestroyRef);

  // --- Inputs ---
  aggregates = input<StockAggregate[]>([]);
  indicators = input<IndicatorSeries[]>([]);
  ticker = input('');

  // --- Derived state ---
  candlestickData = computed<CandlestickData[]>(() =>
    this.aggregates()
      .map(a => ({
        time: toUtc(a.timestamp),
        open: a.open, high: a.high, low: a.low, close: a.close,
      }))
      .sort(byTime)
  );

  overlayIndicators = computed(() =>
    this.indicators().filter(i => OVERLAY_NAMES.has(i.name))
  );

  rsiIndicator = computed(() =>
    this.indicators().find(i => i.name === 'rsi') ?? null
  );

  hasRsi = computed(() => !!this.rsiIndicator());

  // --- View queries ---
  private priceChartEl = viewChild.required<ElementRef<HTMLDivElement>>('priceChartContainer');
  private rsiChartEl = viewChild<ElementRef<HTMLDivElement>>('rsiChartContainer');

  // --- Chart state ---
  private chartReady = signal(false);
  private priceChart!: IChartApi;
  private candleSeries!: ISeriesApi<'Candlestick'>;
  private overlaySeries: ISeriesApi<'Line'>[] = [];
  private rsiChart: IChartApi | null = null;
  private rsiSeries: ISeriesApi<'Line'> | null = null;

  constructor() {
    afterNextRender(() => {
      const el = this.priceChartEl().nativeElement;
      this.priceChart = buildChart(el, 400);
      this.candleSeries = this.priceChart.addSeries(CandlestickSeries, {
        upColor: '#26a69a', downColor: '#ef5350',
        borderUpColor: '#26a69a', borderDownColor: '#ef5350',
        wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      });
      this.attachResize(el, this.priceChart);
      this.chartReady.set(true);
    });

    effect(() => {
      if (!this.chartReady()) return;
      const data = this.candlestickData();
      if (!data.length) return;
      this.candleSeries.setData(data);
      this.priceChart.timeScale().fitContent();
    });

    effect(() => {
      if (!this.chartReady()) return;
      const overlays = this.overlayIndicators();
      this.overlaySeries.forEach(s => this.priceChart.removeSeries(s));
      this.overlaySeries = [];
      for (const ind of overlays) {
        const series = this.priceChart.addSeries(LineSeries, {
          color: INDICATOR_COLORS[ind.name] ?? '#999',
          lineWidth: 2,
          title: `${ind.name.toUpperCase()}(${ind.window})`,
        });
        series.setData(toLineData(ind));
        this.overlaySeries.push(series);
      }
      this.priceChart.timeScale().fitContent();
    });

    effect(() => {
      if (!this.chartReady()) return;
      const rsi = this.rsiIndicator();
      const el = this.rsiChartEl();

      if (!rsi) {
        this.rsiChart?.remove();
        this.rsiChart = null;
        this.rsiSeries = null;
        return;
      }
      if (!el) return;

      if (!this.rsiChart) {
        this.rsiChart = buildChart(el.nativeElement, 200);
        this.attachResize(el.nativeElement, this.rsiChart);
      }

      if (this.rsiSeries) this.rsiChart.removeSeries(this.rsiSeries);
      this.rsiSeries = this.rsiChart.addSeries(LineSeries, {
        color: INDICATOR_COLORS['rsi'],
        lineWidth: 2,
        title: `RSI(${rsi.window})`,
      });
      this.rsiSeries.setData(toLineData(rsi));
      this.rsiChart.timeScale().fitContent();
    });

    this.destroyRef.onDestroy(() => {
      this.priceChart?.remove();
      this.rsiChart?.remove();
    });
  }

  private attachResize(container: HTMLElement, chart: IChartApi): void {
    const observer = new ResizeObserver(([entry]) => {
      chart.applyOptions({ width: entry.contentRect.width });
    });
    observer.observe(container);
    this.destroyRef.onDestroy(() => observer.disconnect());
  }
}

// --- Pure helpers ---

function toUtc(timestamp: string | number): UTCTimestamp {
  const ms = typeof timestamp === 'string' ? new Date(timestamp).getTime() : timestamp;
  return (ms / 1000) as UTCTimestamp;
}

function byTime(a: { time: UTCTimestamp }, b: { time: UTCTimestamp }): number {
  return (a.time as number) - (b.time as number);
}

function toLineData(indicator: IndicatorSeries): LineData[] {
  return indicator.data
    .filter(d => d.value !== null)
    .map(d => ({ time: toUtc(d.timestamp), value: d.value! }))
    .sort(byTime);
}

function buildChart(container: HTMLElement, height: number): IChartApi {
  return createChart(container, {
    width: container.clientWidth,
    height,
    layout: { background: { color: '#ffffff' }, textColor: '#333' },
    grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
    timeScale: { timeVisible: false, borderColor: '#ddd' },
    crosshair: { mode: 0 },
  });
}
