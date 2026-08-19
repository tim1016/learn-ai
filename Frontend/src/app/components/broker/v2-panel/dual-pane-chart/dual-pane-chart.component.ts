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
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type TickMarkType,
  type UTCTimestamp,
  createSeriesMarkers,
} from 'lightweight-charts';
import { rxResource } from '@angular/core/rxjs-interop';
import { catchError, map, of } from 'rxjs';
import type {
  ChartBar,
  ChartFillMarker,
  ChartHistoryTimeframe,
  ChartLiveResolution,
  ChartSource,
} from '../lib/broker-v2-panel.types';
import { toCandle } from '../lib/chart-bar-mapping';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import { createAppChart, formatChartAxisTick } from '../../../../shared/charts/chart-utils';
import { IndicatorCatalogService } from '../../../../shared/indicator-catalog/indicator-catalog.service';
import {
  ChartIndicatorRailComponent,
  type ChartIndicatorColorChange,
  type ChartIndicatorEntry,
  type ChartIndicatorResult,
} from '../../../../shared/trading-chart';
import type { IndicatorPickerAdd } from '../../../../shared/indicator-picker/indicator-picker.component';
import { PanelInstrumentQuoteComponent } from '../instrument-quote/panel-instrument-quote.component';
import { BotChartIndicatorService } from './bot-chart-indicator.service';
import {
  type SelectedChartIndicator,
  resultBelongsToIndicator,
  selectChartIndicator,
  toActiveIndicatorChips,
  toIndicatorSeriesPlans,
} from './dual-pane-chart-indicators';

type ChartPane = 'live' | 'polygon';
export type ChartTimeZone = 'local' | 'et';

interface IndicatorLoadState {
  indicators: readonly ChartIndicatorResult[];
  error: string | null;
  viewKey: string;
}

const TIME_ZONE_STORAGE_KEY = 'broker-v2.chart-timezone.v1';

function persistedChartTimeZone(): ChartTimeZone {
  if (typeof localStorage === 'undefined') return 'local';
  return localStorage.getItem(TIME_ZONE_STORAGE_KEY) === 'et' ? 'et' : 'local';
}

/** Formats the chart-library's seconds-UTC boundary for the crosshair readout. */
export function formatChartCrosshairTime(time: Time | number, timeZone: ChartTimeZone): string {
  if (typeof time !== 'number') return String(time);
  return formatTimestampDisplay(time * 1_000, {
    mode: timeZone === 'et' ? 'et' : 'local',
    granularity: 'chart',
  });
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

interface FillMarkerGroup {
  readonly time: UTCTimestamp;
  readonly side: ChartFillMarker['side'];
  readonly fills: ChartFillMarker[];
}

function markerText(group: FillMarkerGroup): string {
  const side = group.side.toUpperCase();
  if (group.fills.length !== 1) return `${side} · ${group.fills.length} fills`;
  const [fill] = group.fills;
  return `${side} ${fill.quantity} @ ${fill.price}`;
}

/**
 * Count the fills a chart actually represents, which is not the number of
 * marker glyphs it draws: `toSeriesMarkers` collapses same-candle, same-side
 * fills into one arrow. The tape readout reports fills, so counting glyphs
 * under-reported it — two grouped fills plus one off-window fill rendered as
 * "1 plotted / 3 fills".
 */
export function countPlottedFills(
  markers: readonly ChartFillMarker[],
  bars: readonly ChartBar[],
): number {
  let plotted = 0;
  for (const marker of markers) {
    if (markerTime(marker, bars) !== null) plotted += 1;
  }
  return plotted;
}

export function toSeriesMarkers(
  markers: readonly ChartFillMarker[],
  bars: readonly ChartBar[],
): SeriesMarker<UTCTimestamp>[] {
  const groups = new Map<string, FillMarkerGroup>();
  for (const marker of markers) {
      const time = markerTime(marker, bars);
      if (time === null) continue;
      const key = `${time}:${marker.side}`;
      const existing = groups.get(key);
      if (existing) {
        existing.fills.push(marker);
      } else {
        groups.set(key, { time, side: marker.side, fills: [marker] });
      }
  }

  return [...groups.values()]
    .map((group) => {
      const isBuy = group.side === 'buy';
      return {
        time: group.time,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#29b6f6' : '#ff9800',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: markerText(group),
      } satisfies SeriesMarker<UTCTimestamp>;
    })
    .sort((left, right) => left.time - right.time);
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

interface PolygonTimeframeOption {
  value: ChartHistoryTimeframe;
  label: string;
}

export const POLYGON_TIMEFRAMES: readonly PolygonTimeframeOption[] = [
  { value: '1m', label: '1m' },
  { value: '15m', label: '15m' },
  { value: '30m', label: '30m' },
  { value: '1h', label: '1h' },
  { value: '1d', label: '1D' },
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
  imports: [ChartIndicatorRailComponent, PanelInstrumentQuoteComponent, ReceiptLabelPipe],
  templateUrl: './dual-pane-chart.component.html',
  styleUrl: './dual-pane-chart.component.scss',
  host: { '(keydown.escape)': 'collapseFullscreen()' },
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
  readonly histIndicatorBars = input<readonly ChartBar[]>([]);
  readonly histFillMarkers = input<readonly ChartFillMarker[]>([]);
  readonly historyDataTimeframe = input<ChartHistoryTimeframe | null>(null);
  readonly historyTimeframe = input<ChartHistoryTimeframe>('1m');
  readonly histIndicatorBarBudget = input(0);
  readonly histIndicatorBarBudgetSatisfied = input(true);

  readonly historyTimeframeChange = output<ChartHistoryTimeframe>();
  readonly liveResolutionChange = output<ChartLiveResolution>();

  private readonly chartContainer =
    viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');
  private readonly destroyRef = inject(DestroyRef);
  private readonly createChart = inject(DUAL_PANE_CHART_FACTORY);
  private readonly indicatorCatalog = inject(IndicatorCatalogService);
  private readonly indicatorService = inject(BotChartIndicatorService);

  protected readonly activePane = signal<ChartPane>('live');
  protected readonly fullscreen = signal(false);
  protected readonly timeZone = signal<ChartTimeZone>(persistedChartTimeZone());
  private readonly selectedIndicators = signal<readonly SelectedChartIndicator[]>([]);
  private readonly indicatorColorOverrides = signal<Readonly<Record<string, string>>>({});
  protected readonly polygonTimeframes = POLYGON_TIMEFRAMES;
  protected readonly liveResolutions = LIVE_RESOLUTIONS;
  private readonly supportedIndicatorResource = rxResource({
    params: () => 'chart-indicator-catalog',
    stream: () => this.indicatorService.supportedIndicators(),
  });
  protected readonly indicatorCategories = computed(() => {
    const supported = new Set(this.supportedIndicatorResource.value()?.names ?? []);
    return this.indicatorCatalog.categories()
      .map((category) => ({
        ...category,
        indicators: category.indicators.filter((indicator) => supported.has(indicator.name)),
      }))
      .filter((category) => category.indicators.length > 0);
  });
  protected readonly indicatorCatalogLoading = computed(() =>
    this.indicatorCatalog.loading() || this.supportedIndicatorResource.isLoading(),
  );

  protected readonly liveSource = computed<ChartSource | null>(() => {
    const bars = this.liveBars();
    if (!bars.length) return null;
    const sources = new Set(bars.map((bar) => bar.source));
    return sources.size === 1 ? bars[0].source : 'mixed';
  });

  private readonly historyMatchesSelection = computed(() =>
    this.historyDataTimeframe() === this.historyTimeframe(),
  );
  protected readonly activeBars = computed(() => {
    if (this.activePane() === 'live') return this.liveBars();
    return this.historyMatchesSelection() ? this.histBars() : [];
  });
  private readonly activeIndicatorBars = computed(() => {
    if (this.activePane() === 'live') return this.liveBars();
    if (!this.historyMatchesSelection()) return [];
    return this.histIndicatorBars();
  });
  protected readonly activeMarkers = computed(() => {
    if (this.activePane() === 'live') return this.liveFillMarkers();
    return this.historyMatchesSelection() ? this.histFillMarkers() : [];
  });
  protected readonly activeLoading = computed(() =>
    this.activePane() === 'live' ? this.liveLoading() : this.historyLoading(),
  );
  protected readonly visibleFillCount = computed(() =>
    countPlottedFills(this.activeMarkers(), this.activeBars()),
  );
  protected readonly activeIndicatorKeys = computed(() =>
    this.selectedIndicators().map((indicator) => indicator.name),
  );
  private readonly indicatorViewKey = computed(() =>
    this.activePane() === 'live'
      ? `live:${this.liveResolution()}`
      : `polygon:${this.historyTimeframe()}`,
  );
  private readonly indicatorResource = rxResource<IndicatorLoadState, {
    symbol: string;
    bars: readonly ChartBar[];
    indicators: readonly ChartIndicatorEntry[];
    viewKey: string;
  }>({
    params: () => ({
      symbol: this.symbol(),
      bars: this.activeIndicatorBars(),
      indicators: this.selectedIndicators(),
      viewKey: this.indicatorViewKey(),
    }),
    stream: ({ params }) => {
      if (params.bars.length === 0 || params.indicators.length === 0) {
        return of<IndicatorLoadState>({ indicators: [], error: null, viewKey: params.viewKey });
      }
      return this.indicatorService.calculate(params.symbol, params.bars, params.indicators).pipe(
        map((response): IndicatorLoadState => ({
          indicators: response.indicators,
          error: null,
          viewKey: params.viewKey,
        })),
        catchError(() => of<IndicatorLoadState>({
          indicators: [],
          error: 'Indicators could not be calculated for this candle window.',
          viewKey: params.viewKey,
        })),
      );
    },
  });
  protected readonly indicatorCalculationLoading = computed(() =>
    this.selectedIndicators().length > 0 && this.indicatorResource.isLoading(),
  );
  protected readonly indicatorError = computed(() => {
    if (this.supportedIndicatorResource.error()) {
      return 'The chart indicator catalog could not be loaded.';
    }
    return this.indicatorResource.value()?.error ?? null;
  });
  private readonly loadedIndicatorResults = signal<readonly ChartIndicatorResult[]>([]);
  private readonly loadedIndicatorViewKey = signal<string | null>(null);
  private readonly renderedIndicatorResults = computed(() =>
    this.loadedIndicatorViewKey() === this.indicatorViewKey()
      ? this.loadedIndicatorResults()
      : [],
  );
  protected readonly activeIndicatorChips = computed(() =>
    toActiveIndicatorChips(
      this.selectedIndicators(),
      this.renderedIndicatorResults(),
      this.indicatorColorOverrides(),
    ),
  );
  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Candlestick'> | null = null;
  private markers: ISeriesMarkersPluginApi<Time> | null = null;
  private indicatorSeries: ISeriesApi<SeriesType>[] = [];
  private renderedViewKey: string | null = null;
  private renderedBars: readonly ChartBar[] = [];

  constructor() {
    void this.indicatorCatalog.load();
    effect(() => this.renderActivePane());
    effect(() => {
      const selected = this.selectedIndicators();
      const loaded = this.indicatorResource.value();
      if (selected.length === 0) {
        this.loadedIndicatorResults.set([]);
        this.loadedIndicatorViewKey.set(null);
      } else if (loaded?.error === null && loaded.viewKey === this.indicatorViewKey()) {
        this.loadedIndicatorResults.set(loaded.indicators);
        this.loadedIndicatorViewKey.set(loaded.viewKey);
      }
    });
    effect(() => this.renderIndicators(
      this.renderedIndicatorResults(),
      this.activeBars(),
      this.selectedIndicators(),
      this.indicatorColorOverrides(),
    ));
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
    this.renderIndicators(
      this.renderedIndicatorResults(),
      this.activeBars(),
      this.selectedIndicators(),
      this.indicatorColorOverrides(),
    );
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

  protected selectHistoryTimeframe(timeframe: ChartHistoryTimeframe): void {
    this.historyTimeframeChange.emit(timeframe);
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

  protected collapseFullscreen(): void {
    if (!this.fullscreen()) return;
    this.fullscreen.set(false);
    requestAnimationFrame(() => this.chart?.timeScale().fitContent());
  }

  protected addIndicator(entry: IndicatorPickerAdd): void {
    this.selectedIndicators.update((current) => selectChartIndicator(current, entry));
  }

  protected removeIndicator(id: string): void {
    const removed = this.selectedIndicators().find((indicator) => indicator.id === id);
    this.selectedIndicators.update((current) => current.filter((indicator) => indicator.id !== id));
    if (removed) {
      this.loadedIndicatorResults.update((results) =>
        results.filter((result) => !resultBelongsToIndicator(result, removed)),
      );
    }
    this.indicatorColorOverrides.update((overrides) => {
      if (!(id in overrides)) return overrides;
      return Object.fromEntries(Object.entries(overrides).filter(([key]) => key !== id));
    });
  }

  protected changeIndicatorColor(change: ChartIndicatorColorChange): void {
    this.indicatorColorOverrides.update((overrides) => ({
      ...overrides,
      [change.id]: change.color,
    }));
  }

  private renderActivePane(): void {
    const bars = this.activeBars();
    const fillMarkers = this.activeMarkers();
    const pane = this.activePane();
    const liveSource = this.liveSource();
    const viewKey = pane === 'live'
      ? `${this.symbol()}:live:${this.liveResolution()}`
      : `${this.symbol()}:polygon:${this.historyTimeframe()}`;
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
    this.indicatorSeries = [];
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

  private renderIndicators(
    results: readonly ChartIndicatorResult[],
    bars: readonly ChartBar[],
    selected: readonly SelectedChartIndicator[],
    colorOverrides: Readonly<Record<string, string>>,
  ): void {
    if (!this.chart) return;
    for (const rendered of this.indicatorSeries) this.chart.removeSeries(rendered);
    this.indicatorSeries = [];

    const paneIndices = new Map<string, number>();
    for (const plan of toIndicatorSeriesPlans(results, bars, selected, colorOverrides)) {
      let paneIndex = 0;
      if (plan.pane !== 'main') {
        const existing = paneIndices.get(plan.pane);
        paneIndex = existing ?? paneIndices.size + 1;
        paneIndices.set(plan.pane, paneIndex);
      }
      const rendered = plan.type === 'histogram'
        ? this.chart.addSeries(HistogramSeries, {
            color: plan.color,
            priceLineVisible: false,
            lastValueVisible: false,
          }, paneIndex)
        : this.chart.addSeries(LineSeries, {
            color: plan.color,
            lineWidth: 2,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
          }, paneIndex);
      rendered.setData(plan.points.map((point) => ({
        time: point.time,
        value: point.value,
      })));
      for (const price of plan.referenceLevels) {
        rendered.createPriceLine({
          price,
          color: '#4a5068',
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
        });
      }
      this.indicatorSeries.push(rendered);
    }
  }
}
