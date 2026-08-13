import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  InjectionToken,
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
  type TickMarkType,
  type UTCTimestamp,
  createSeriesMarkers,
} from 'lightweight-charts';
import type {
  ChartBar,
  ChartFillMarker,
  ChartHistoryPreset,
  ChartLiveResolution,
  ChartSource,
} from '../lib/broker-v2-panel.types';
import { toCandle } from '../lib/chart-bar-mapping';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import { createAppChart, formatChartAxisTick } from '../../../../shared/charts/chart-utils';
import { PanelInstrumentQuoteComponent } from '../instrument-quote/panel-instrument-quote.component';

type ChartPane = 'live' | 'polygon';
export type ChartTimeZone = 'local' | 'et';

const TIME_ZONE_STORAGE_KEY = 'broker-v2.chart-timezone.v1';

function persistedChartTimeZone(): ChartTimeZone {
  if (typeof localStorage === 'undefined') return 'local';
  return localStorage.getItem(TIME_ZONE_STORAGE_KEY) === 'et' ? 'et' : 'local';
}

/** Formats the chart-library's seconds-UTC boundary for the crosshair readout. */
export function formatChartCrosshairTime(time: Time | number, timeZone: ChartTimeZone): string {
  if (typeof time !== 'number') return String(time);
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZone: timeZone === 'et' ? 'America/New_York' : undefined,
  }).format(new Date(time * 1_000));
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
export const DUAL_PANE_CHART_FACTORY = new InjectionToken<typeof createAppChart>(
  'DUAL_PANE_CHART_FACTORY',
  { providedIn: 'root', factory: () => createAppChart },
);

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
  imports: [PanelInstrumentQuoteComponent, ReceiptLabelPipe],
  templateUrl: './dual-pane-chart.component.html',
  styleUrl: './dual-pane-chart.component.scss',
})
export class DualPaneChartComponent implements AfterViewInit {
  readonly symbol = input.required<string>();
  readonly tickerQuote = input<TickerQuoteView | null>(null);
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
  private readonly createChart = inject(DUAL_PANE_CHART_FACTORY);

  protected readonly activePane = signal<ChartPane>('live');
  protected readonly fullscreen = signal(false);
  protected readonly timeZone = signal<ChartTimeZone>(persistedChartTimeZone());
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
  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Candlestick'> | null = null;
  private markers: ISeriesMarkersPluginApi<Time> | null = null;
  private renderedViewKey: string | null = null;
  private renderedBars: readonly ChartBar[] = [];

  constructor() {
    effect(() => this.renderActivePane());
    effect(() => {
      this.timeZone();
      this.applyTimeZoneFormatting();
    });
  }

  ngAfterViewInit(): void {
    this.chart = this.createChart(this.chartContainer().nativeElement, {
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
    this.applyTimeZoneFormatting();
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

  protected selectTimeZone(timeZone: ChartTimeZone): void {
    this.timeZone.set(timeZone);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(TIME_ZONE_STORAGE_KEY, timeZone);
    }
  }

  protected toggleFullscreen(): void {
    this.fullscreen.update((value) => !value);
    requestAnimationFrame(() => this.chart?.timeScale().fitContent());
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
    for (let index = this.renderedBars.length; index < bars.length; index += 1) {
      const previous = bars[index - 1];
      const appended = bars[index];
      if (
        previous === undefined
        || appended === undefined
        || appended.start_ms !== previous.end_ms
        || appended.end_ms <= appended.start_ms
      ) {
        return true;
      }
    }
    return false;
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

  private applyTimeZoneFormatting(): void {
    const timeZone = this.timeZone();
    this.chart?.applyOptions({
      localization: {
        timeFormatter: (time: Time) => formatChartCrosshairTime(time, timeZone),
      },
      timeScale: {
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => formatChartAxisTick(
          time,
          timeZone === 'et' ? 'America/New_York' : undefined,
          tickMarkType,
        ),
      },
    });
  }
}
