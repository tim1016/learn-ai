import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import {
  CandlestickSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';
import type {
  ChartBar,
  ChartFillMarker,
  ChartHistoryPreset,
  ChartLiveResolution,
  ChartSource,
} from '../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

type ChartPane = 'live' | 'polygon';

/** Map a millisecond UTC ChartBar to lightweight-charts candle data. */
function toCandle(bar: ChartBar): {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
} {
  return {
    time: Math.floor(bar.start_ms / 1000) as UTCTimestamp,
    open: Number(bar.open),
    high: Number(bar.high),
    low: Number(bar.low),
    close: Number(bar.close),
  };
}

function sameBar(left: ChartBar, right: ChartBar): boolean {
  return left.start_ms === right.start_ms
    && left.end_ms === right.end_ms
    && left.open === right.open
    && left.high === right.high
    && left.low === right.low
    && left.close === right.close
    && left.volume === right.volume
    && left.source === right.source;
}

/** Return the exact candle whose half-open interval contains the fill. */
function markerTime(
  marker: ChartFillMarker,
  bars: readonly ChartBar[],
): UTCTimestamp | null {
  for (const bar of bars) {
    if (bar.start_ms > marker.filled_at_ms) break;
    if (marker.filled_at_ms < bar.end_ms) {
      return Math.floor(bar.start_ms / 1000) as UTCTimestamp;
    }
  }
  return null;
}

export function toSeriesMarkers(
  markers: readonly ChartFillMarker[],
  bars: readonly ChartBar[],
): SeriesMarker<UTCTimestamp>[] {
  return markers
    .flatMap((marker) => {
      const time = markerTime(marker, bars);
      if (time === null) return [];
      const isBuy = marker.side === 'buy';
      return [{
        time,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#29b6f6' : '#ff9800',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: `${marker.side.toUpperCase()} ${marker.quantity} @ ${marker.price}`,
      } satisfies SeriesMarker<UTCTimestamp>];
    })
    .sort((a, b) => a.time - b.time);
}

function sourceColors(source: ChartSource): {
  upColor: string;
  downColor: string;
  borderUpColor: string;
  borderDownColor: string;
  wickUpColor: string;
  wickDownColor: string;
} {
  if (source === 'polygon') {
    return {
      upColor: '#5eead4',
      downColor: '#fb7185',
      borderUpColor: '#2dd4bf',
      borderDownColor: '#f43f5e',
      wickUpColor: '#2dd4bf',
      wickDownColor: '#f43f5e',
    };
  }
  return {
    upColor: '#26a69a',
    downColor: '#ef5350',
    borderUpColor: '#26a69a',
    borderDownColor: '#ef5350',
    wickUpColor: '#26a69a',
    wickDownColor: '#ef5350',
  };
}

export const HISTORY_PRESETS: readonly ChartHistoryPreset[] = [
  '1D', '5D', '1M', '3M', '1Y', 'All',
];
export const LIVE_RESOLUTIONS: readonly ChartLiveResolution[] = ['5s', '1m'];

/**
 * Source-tabbed market tape for one bot symbol.
 *
 * One chart instance keeps the price canvas spacious while a source rail
 * switches between IBKR live bars and Polygon's delayed archive. The live
 * interval and Polygon range are independent controls. Fill markers are
 * projected onto the containing live candle using int64 millisecond UTC input;
 * conversion to the chart library's seconds happens only at the render edge.
 */
@Component({
  selector: 'app-dual-pane-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './dual-pane-chart.component.html',
  styleUrl: './dual-pane-chart.component.scss',
})
export class DualPaneChartComponent implements AfterViewInit {
  readonly symbol = input.required<string>();
  readonly liveBars = input<readonly ChartBar[]>([]);
  readonly liveFillMarkers = input<readonly ChartFillMarker[]>([]);
  readonly liveNotices = input<readonly { code: string; message: string }[]>([]);
  readonly liveLoading = input(false);
  readonly historyLoading = input(false);
  readonly liveResolution = input<ChartLiveResolution>('5s');
  readonly histBars = input<readonly ChartBar[]>([]);
  readonly histFillMarkers = input<readonly ChartFillMarker[]>([]);
  readonly selectedPreset = input<ChartHistoryPreset>('1D');

  readonly presetChange = output<ChartHistoryPreset>();
  readonly liveResolutionChange = output<ChartLiveResolution>();

  private readonly chartContainer =
    viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');
  private readonly destroyRef = inject(DestroyRef);

  protected readonly activePane = signal<ChartPane>('live');
  protected readonly fullscreen = signal(false);
  protected readonly presets = HISTORY_PRESETS;
  protected readonly liveResolutions = LIVE_RESOLUTIONS;

  protected readonly liveSource = computed<ChartSource | null>(() => {
    const bars = this.liveBars();
    if (!bars.length) return null;
    const sources = new Set(bars.map((bar) => bar.source));
    return sources.size === 1 ? bars[0].source : 'mixed';
  });

  protected readonly activeBars = computed(() =>
    this.activePane() === 'live' ? this.liveBars() : this.histBars(),
  );
  protected readonly activeMarkers = computed(() =>
    this.activePane() === 'live' ? this.liveFillMarkers() : this.histFillMarkers(),
  );
  protected readonly activeLoading = computed(() =>
    this.activePane() === 'live' ? this.liveLoading() : this.historyLoading(),
  );
  protected readonly visibleFillCount = computed(() =>
    toSeriesMarkers(this.activeMarkers(), this.activeBars()).length,
  );
  protected readonly lastPrice = computed(() => {
    const bars = this.activeBars();
    return bars.length ? Number(bars[bars.length - 1].close) : null;
  });

  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Candlestick'> | null = null;
  private markers: ISeriesMarkersPluginApi<Time> | null = null;
  private renderedViewKey: string | null = null;
  private renderedBars: readonly ChartBar[] = [];

  constructor() {
    effect(() => this.renderActivePane());
  }

  ngAfterViewInit(): void {
    this.chart = createChart(this.chartContainer().nativeElement, {
      layout: {
        background: { color: 'transparent' },
        textColor: '#9598a1',
        fontFamily: 'JetBrains Mono, SFMono-Regular, Consolas, monospace',
      },
      grid: {
        vertLines: { color: 'rgba(42, 46, 57, 0.55)' },
        horzLines: { color: 'rgba(42, 46, 57, 0.55)' },
      },
      rightPriceScale: { borderColor: '#2a2e39' },
      timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: true },
      autoSize: true,
    });
    this.series = this.chart.addSeries(CandlestickSeries, {});
    this.markers = createSeriesMarkers(this.series, []);
    this.renderActivePane();
    this.destroyRef.onDestroy(() => this.cleanup());
  }

  protected selectPane(pane: ChartPane): void {
    this.activePane.set(pane);
  }

  protected onTabKeydown(event: KeyboardEvent): void {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const pane = event.key === 'ArrowLeft' || event.key === 'Home' ? 'live' : 'polygon';
    this.activePane.set(pane);
    const target = (event.currentTarget as HTMLElement | null)?.parentElement?.querySelector(
      `[data-chart-pane="${pane}"]`,
    );
    if (target instanceof HTMLElement) target.focus();
  }

  protected selectLiveResolution(resolution: ChartLiveResolution): void {
    this.liveResolutionChange.emit(resolution);
  }

  protected selectPreset(preset: ChartHistoryPreset): void {
    this.presetChange.emit(preset);
  }

  protected toggleFullscreen(): void {
    this.fullscreen.update((value) => !value);
    requestAnimationFrame(() => this.chart?.timeScale().fitContent());
  }

  protected formatPrice(price: number): string {
    return price.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    });
  }

  private renderActivePane(): void {
    const bars = this.activeBars();
    const fillMarkers = this.activeMarkers();
    const pane = this.activePane();
    const liveSource = this.liveSource();
    const viewKey = pane === 'live'
      ? `${this.symbol()}:live:${this.liveResolution()}`
      : `${this.symbol()}:polygon:${this.selectedPreset()}`;
    if (!this.series) return;
    this.markers?.setMarkers(toSeriesMarkers(fillMarkers, bars));
    const source = pane === 'polygon' ? 'polygon' : (liveSource ?? 'ibkr');
    this.series.applyOptions(sourceColors(source));
    const fullReplace = this.requiresFullReplace(viewKey, bars);
    if (fullReplace) {
      this.series.setData(bars.map(toCandle));
    } else {
      const changedFrom = this.firstChangedBar(this.renderedBars, bars);
      for (const bar of bars.slice(changedFrom)) this.series.update(toCandle(bar));
    }
    const shouldFit = bars.length > 0
      && (viewKey !== this.renderedViewKey || this.renderedBars.length === 0);
    if (shouldFit) this.chart?.timeScale().fitContent();
    this.renderedViewKey = viewKey;
    this.renderedBars = [...bars];
  }

  private requiresFullReplace(viewKey: string, bars: readonly ChartBar[]): boolean {
    if (viewKey !== this.renderedViewKey || this.renderedBars.length === 0) return true;
    if (bars.length < this.renderedBars.length) return true;
    const changedFrom = this.firstChangedBar(this.renderedBars, bars);
    if (changedFrom < Math.max(0, this.renderedBars.length - 1)) return true;
    const firstAppended = bars[this.renderedBars.length];
    const previousLast = this.renderedBars.at(-1);
    return firstAppended !== undefined
      && previousLast !== undefined
      && firstAppended.start_ms > previousLast.end_ms;
  }

  private firstChangedBar(
    previous: readonly ChartBar[],
    next: readonly ChartBar[],
  ): number {
    const sharedLength = Math.min(previous.length, next.length);
    for (let index = 0; index < sharedLength; index += 1) {
      if (!sameBar(previous[index], next[index])) return index;
    }
    return sharedLength;
  }

  private cleanup(): void {
    this.chart?.remove();
    this.chart = null;
    this.series = null;
    this.markers = null;
    this.renderedViewKey = null;
    this.renderedBars = [];
  }
}
