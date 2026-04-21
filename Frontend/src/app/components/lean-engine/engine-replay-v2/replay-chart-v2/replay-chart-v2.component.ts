import {
  Component, ChangeDetectionStrategy, ElementRef, afterNextRender,
  inject, effect, DestroyRef, viewChild, computed,
} from '@angular/core';
import {
  createChart, IChartApi, ISeriesApi, CandlestickSeries, LineSeries,
  CandlestickData, LineData, UTCTimestamp, Time,
  SeriesMarker, createSeriesMarkers, ISeriesMarkersPluginApi,
  CrosshairMode, PriceLineOptions, LineStyle, IPriceLine,
} from 'lightweight-charts';
import { formatTickMark } from '../../../market-data/chart-utils';
import { ReplayEngineV2Service } from '../services/replay-engine-v2.service';

const INDICATOR_COLORS = ['#ff9f43', '#54a0ff', '#c56cf0', '#1dd1a1', '#ee5253'];

@Component({
  selector: 'app-replay-chart-v2',
  standalone: true,
  templateUrl: './replay-chart-v2.component.html',
  styleUrls: ['./replay-chart-v2.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReplayChartV2Component {
  private readonly svc = inject(ReplayEngineV2Service);
  private readonly destroyRef = inject(DestroyRef);
  readonly container = viewChild.required<ElementRef<HTMLDivElement>>('container');

  private chart: IChartApi | null = null;
  private candles: ISeriesApi<'Candlestick'> | null = null;
  private indicatorSeries: ISeriesApi<'Line'>[] = [];
  private markers: ISeriesMarkersPluginApi<Time> | null = null;
  private activeLine: IPriceLine | null = null;
  private resizeObserver: ResizeObserver | null = null;

  readonly hidden = this.svc.hiddenSummary;
  readonly window = this.svc.renderWindow;

  readonly leftHiddenLabel = computed(() => {
    const h = this.hidden();
    if (h.leftCount === 0) return '';
    const pnl = h.leftCumPnl;
    const sign = pnl >= 0 ? '+' : '';
    return `← ${h.leftCount} hidden · ${sign}${pnl.toFixed(2)}`;
  });

  readonly rightHiddenLabel = computed(() => {
    const h = this.hidden();
    if (h.rightCount === 0) return '';
    const pnl = h.rightCumPnl;
    const sign = pnl >= 0 ? '+' : '';
    return `${h.rightCount} ahead · ${sign}${pnl.toFixed(2)} →`;
  });

  constructor() {
    afterNextRender(() => this.initChart());

    effect(() => {
      this.window();
      if (this.chart) this.syncCandles();
    });
    effect(() => {
      this.svc.visibleIndicatorsWindow();
      if (this.chart) this.syncIndicators();
    });
    effect(() => {
      this.svc.windowTrades();
      this.svc.currentMs();
      if (this.chart) this.syncMarkers();
    });
    effect(() => {
      this.svc.activePosition();
      this.svc.currentBar();
      if (this.chart) this.syncActiveLine();
    });

    this.destroyRef.onDestroy(() => {
      this.resizeObserver?.disconnect();
      this.chart?.remove();
      this.chart = null;
    });
  }

  private initChart(): void {
    const el = this.container().nativeElement;
    this.chart = createChart(el, {
      width: el.clientWidth,
      height: 420,
      layout: {
        background: { color: 'transparent' },
        textColor: '#cbd5e1',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: 'rgba(148, 163, 184, 0.08)' },
        horzLines: { color: 'rgba(148, 163, 184, 0.08)' },
      },
      rightPriceScale: { borderColor: 'rgba(148, 163, 184, 0.2)' },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: 'rgba(148, 163, 184, 0.2)',
        tickMarkFormatter: formatTickMark,
        rightOffset: 2,
        shiftVisibleRangeOnNewBar: false,
      },
      crosshair: { mode: CrosshairMode.Normal },
    });

    this.candles = this.chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
    });
    this.markers = createSeriesMarkers(this.candles, []);

    this.syncCandles();
    this.syncIndicators();
    this.syncMarkers();
    this.syncActiveLine();

    this.resizeObserver = new ResizeObserver(entries => {
      if (!this.chart || !entries.length) return;
      const { width } = entries[0].contentRect;
      this.chart.applyOptions({ width });
    });
    this.resizeObserver.observe(el);
  }

  private applyVisibleRange(): void {
    if (!this.chart) return;
    const size = this.svc.windowSize();
    const win = this.window();
    const dataLen = win.bars.length;
    if (dataLen === 0) return;
    const ts = this.chart.timeScale();
    if (size === 'all') {
      ts.fitContent();
      return;
    }
    // Lock visible logical range to exactly `size` units. Negative `from`
    // lets the right edge stay pinned even before the window has filled.
    const rightGutter = 3;
    const from = dataLen - size;
    const to = dataLen - 1 + rightGutter;
    ts.setVisibleLogicalRange({ from, to });
  }

  private syncCandles(): void {
    if (!this.candles || !this.chart) return;
    const win = this.window();
    if (win.bars.length === 0) {
      this.candles.setData([]);
      return;
    }
    const data: CandlestickData[] = win.bars.map(b => ({
      time: (new Date(b.timestamp).getTime() / 1000) as UTCTimestamp,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }));
    this.candles.setData(data);
    this.applyVisibleRange();
  }

  private syncIndicators(): void {
    if (!this.chart) return;
    for (const s of this.indicatorSeries) this.chart.removeSeries(s);
    this.indicatorSeries = [];
    const list = this.svc.visibleIndicatorsWindow();
    list.forEach((ind, i) => {
      const color = INDICATOR_COLORS[i % INDICATOR_COLORS.length];
      const line = this.chart!.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        title: `${ind.name.toUpperCase()}(${ind.window})`,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      const data: LineData[] = ind.data.map(p => ({
        time: (p.timestamp / 1000) as UTCTimestamp,
        value: p.value as number,
      }));
      line.setData(data);
      this.indicatorSeries.push(line);
    });
  }

  private syncMarkers(): void {
    if (!this.markers) return;
    const trades = this.svc.windowTrades();
    const nowMs = this.svc.currentMs();
    const out: SeriesMarker<Time>[] = [];
    for (const t of trades) {
      const isShort = /short/i.test(t.tradeType);
      if (t.entryMs <= nowMs) {
        out.push({
          time: (t.entryMs / 1000) as UTCTimestamp,
          position: isShort ? 'aboveBar' : 'belowBar',
          color: isShort ? '#f87171' : '#22c55e',
          shape: isShort ? 'arrowDown' : 'arrowUp',
          text: isShort ? `SELL #${t.tradeNumber}` : `BUY #${t.tradeNumber}`,
        });
      }
      if (t.exitMs <= nowMs) {
        out.push({
          time: (t.exitMs / 1000) as UTCTimestamp,
          position: isShort ? 'belowBar' : 'aboveBar',
          color: t.pnl >= 0 ? '#4ade80' : '#ef4444',
          shape: 'circle',
          text: `CLOSE ${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}`,
        });
      }
    }
    out.sort((a, b) => (a.time as number) - (b.time as number));
    this.markers.setMarkers(out);
  }

  private syncActiveLine(): void {
    if (!this.candles) return;
    if (this.activeLine) {
      this.candles.removePriceLine(this.activeLine);
      this.activeLine = null;
    }
    const pos = this.svc.activePosition();
    if (!pos) return;
    const opts: PriceLineOptions = {
      price: pos.entryPrice,
      color: '#fbbf24',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: `ENTRY ${pos.entryPrice.toFixed(2)}`,
      lineVisible: true,
      axisLabelColor: '#0f172a',
      axisLabelTextColor: '#fbbf24',
    };
    this.activeLine = this.candles.createPriceLine(opts);
  }
}
