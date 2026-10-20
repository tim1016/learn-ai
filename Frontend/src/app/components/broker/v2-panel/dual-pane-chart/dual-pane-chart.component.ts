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
  linkedSignal,
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
  type ITimeScaleApi,
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
  ChartFeedView,
  ChartFillMarker,
  ChartHistoryTimeframe,
  ChartLiveResolution,
  ChartOverlayNoticeView,
  ChartPositionPane,
  ChartPremiumPane,
  ChartRuleLine,
  ChartSource,
} from '../lib/broker-v2-panel.types';
import { toCandle } from '../lib/chart-bar-mapping';
import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import { createAppChart, formatChartAxisTick } from '../../../../shared/charts/chart-utils';
import {
  ChartIndicatorRailComponent,
  type ChartIndicatorColorChange,
  type ChartIndicatorEntry,
  type ChartIndicatorResult,
} from '../../../../shared/trading-chart';
import type { IndicatorPickerAdd } from '../../../../shared/indicator-picker/indicator-picker.component';
import { PanelInstrumentQuoteComponent } from '../instrument-quote/panel-instrument-quote.component';
import { placeLanes, type LaneSource, type PlacedLane } from '../strategy-view/chart-lanes';
import { ChartLanesComponent } from '../strategy-view/chart-lanes.component';
import type { LogicalCoordinateScale } from '../strategy-view/strategy-chart-overlay';
import { BotChartIndicatorService } from './bot-chart-indicator.service';
import { chartIndicatorCatalog } from './chart-indicator-catalog';
import { positionClock, positionRuns, type PositionRun } from './dual-pane-chart-position';
import { premiumPanePlans, ruleLinePlans } from './premium-pane-plan';
import { OptionContractComponent, optionContractName } from '../../../../shared/option-contract/option-contract.component';
import { optionContractFromSymbol } from '../../../../shared/option-contract/option-contract-symbol';
import {
  type SelectedChartIndicator,
  indicatorFailureMessage,
  resultBelongsToIndicator,
  selectChartIndicator,
  toActiveIndicatorChips,
  toIndicatorSeriesPlans,
} from './dual-pane-chart-indicators';

export type ChartPane = 'live' | 'polygon';
export type ChartTimeZone = 'local' | 'et';

interface IndicatorLoadState {
  indicators: readonly ChartIndicatorResult[];
  error: string | null;
  viewKey: string;
}

const TIME_ZONE_STORAGE_KEY = 'broker-v2.chart-timezone.v1';

function persistedChartTimeZone(): ChartTimeZone {
  try {
    return globalThis.localStorage?.getItem(TIME_ZONE_STORAGE_KEY) === 'et' ? 'et' : 'local';
  } catch {
    // Storage unreadable (blocked site data): the chart opens on the viewer's local clock.
    return 'local';
  }
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
function markerTime<Bar extends Pick<ChartBar, 'start_ms' | 'end_ms'>>(
  marker: ChartFillMarker,
  bars: readonly Bar[],
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
  if (group.fills.some(fill => fill.symbol && optionContractFromSymbol(fill.symbol))) {
    return group.fills.map(fill => {
      const contract = fill.symbol ? optionContractFromSymbol(fill.symbol) : null;
      return `${side} ${fill.quantity} ${contract ? optionContractName(contract) : fill.symbol ?? ''} @ ${fill.price}`;
    }).join(' · ');
  }
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
export function countPlottedFills<Bar extends Pick<ChartBar, 'start_ms' | 'end_ms'>>(
  markers: readonly ChartFillMarker[],
  bars: readonly Bar[],
): number {
  let plotted = 0;
  for (const marker of markers) {
    if (markerTime(marker, bars) !== null) plotted += 1;
  }
  return plotted;
}

export function toSeriesMarkers<Bar extends Pick<ChartBar, 'start_ms' | 'end_ms'>>(
  markers: readonly ChartFillMarker[],
  bars: readonly Bar[],
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
const POSITION_LINE_COLOR = '#c4b5fd';
export const DUAL_PANE_CHART_FACTORY = new InjectionToken<typeof createAppChart>(
  'DUAL_PANE_CHART_FACTORY',
  { providedIn: 'root', factory: () => createAppChart },
);

/**
 * The tape's time scale, read by a bar's place among the candles.
 *
 * An indicator line can hold times the candles do not (its warmup bars), so
 * the library's own logical index is not a candle's index: each bar is found
 * by its time. Up to a bar past either end continues at the bar spacing.
 */
function barCoordinateScale(timeScale: ITimeScaleApi<Time>, bars: readonly ChartBar[]): LogicalCoordinateScale {
  const xOf = (index: number): number | null => timeScale.timeToCoordinate(toCandle(bars[index]).time);
  return {
    logicalToCoordinate: (logical) => {
      if (bars.length === 0) return null;
      if (logical >= 0 && logical < bars.length) return xOf(logical);
      const edge = logical < 0 ? 0 : bars.length - 1;
      const at = xOf(edge);
      return at === null ? null : at + (logical - edge) * timeScale.options().barSpacing;
    },
  };
}

/**
 * Source-tabbed market tape for one bot symbol.
 *
 * One chart instance keeps the price canvas spacious while a source rail
 * switches between IBKR live bars and Polygon's delayed archive. The live
 * interval and Polygon range are independent controls. Fill markers are
 * projected onto the containing live candle using int64 millisecond UTC input;
 * conversion to the chart library's seconds happens only at the render edge.
 *
 * On the bot page the run's lanes sit under the chart, placed on whichever
 * pane is showing: the live bars or the delayed tape (#2808).
 */
@Component({
  selector: 'app-dual-pane-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    ChartIndicatorRailComponent,
    ChartLanesComponent,
    OptionContractComponent,
    PanelInstrumentQuoteComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
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
  /** The LIVE pane's own IBKR bar line (#2355). It is a separate line from
   * the bot's feed, so the pane reports it instead of freezing silently. */
  readonly liveFeed = input<ChartFeedView | null>(null);
  readonly liveLoading = input(false);
  readonly historyLoading = input(false);
  /** Settled Polygon-history failure (#2202 FR-005): renders the delayed
   * pane's unavailable state instead of an indefinite spinner. */
  readonly historyFailed = input(false);
  /** A zero-bar history response carrying a notice outside the closed
   * "genuinely empty" set (#2211): also renders the unavailable state, with
   * this backend-authored message instead of the generic rejected-request
   * copy. `null` means this particular cause does not apply — a rejected
   * request (`historyFailed`) can still make the pane unavailable. */
  readonly historyUnavailableNotice = input<ChartOverlayNoticeView | null>(null);
  readonly liveResolution = input<ChartLiveResolution>('5s');
  readonly histBars = input<readonly ChartBar[]>([]);
  readonly histIndicatorBars = input<readonly ChartBar[]>([]);
  readonly histFillMarkers = input<readonly ChartFillMarker[]>([]);
  readonly historyDataTimeframe = input<ChartHistoryTimeframe | null>(null);
  readonly historyTimeframe = input<ChartHistoryTimeframe>('1m');
  readonly histIndicatorBarBudget = input(0);
  readonly histIndicatorBarBudgetSatisfied = input(true);
  /** What the bot trades, on each tape's own bars, when that is not what it
   * listens to (#2915): a pane under the indicators, sharing the time axis. */
  readonly livePosition = input<ChartPositionPane | null>(null);
  readonly histPosition = input<ChartPositionPane | null>(null);
  readonly livePremiumPanes = input<readonly ChartPremiumPane[]>([]);
  readonly histPremiumPanes = input<readonly ChartPremiumPane[]>([]);
  readonly livePremiumNotice = input<string | null>(null);
  readonly histPremiumNotice = input<string | null>(null);
  readonly liveBacktestNotice = input<string | null>(null);
  readonly histBacktestNotice = input<string | null>(null);
  readonly fixedMinute = input(false);
  readonly liveRuleLines = input<readonly ChartRuleLine[]>([]);
  readonly histRuleLines = input<readonly ChartRuleLine[]>([]);
  protected readonly activePremiumPanes = computed(() => this.activePane() === 'live'
    ? this.livePremiumPanes() : this.histPremiumPanes());
  protected readonly premiumNotice = computed(() => this.activePane() === 'live'
    ? this.livePremiumNotice() : this.histPremiumNotice());
  protected readonly backtestNotice = computed(() => this.activePane() === 'live'
    ? this.liveBacktestNotice() : this.histBacktestNotice());
  protected readonly premiumCaptions = computed(() => this.activePremiumPanes().map((pane) => ({
    ...pane, identity: optionContractFromSymbol(pane.symbol, pane.contract.root),
  })));
  protected readonly premiumCaptionTops = signal<Readonly<Record<string, number>>>({});
  private premiumFirstPane = 1;

  readonly historyTimeframeChange = output<ChartHistoryTimeframe>();
  readonly liveResolutionChange = output<ChartLiveResolution>();
  /** One explicit retry per click (#2202 FR-005/FR-006) — no automatic
   * retry loop; the shell owns the resource and issues the reload. */
  readonly historyRetry = output();

  private readonly chartContainer =
    viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');
  private readonly destroyRef = inject(DestroyRef);
  private readonly createChart = inject(DUAL_PANE_CHART_FACTORY);
  private readonly indicatorService = inject(BotChartIndicatorService);

  /** The pane the chart opens on: the bot page opens a finished run on its delayed 1-minute tape (#2794 R7). */
  readonly initialPane = input<ChartPane>('live');
  /** The run's events, drawn under the chart on the active pane's bars and labelled in the
   * tape's own Local or ET setting (#2808). The Strategy Lab passes none. */
  readonly lanes = input<LaneSource | null>(null);
  protected readonly activePane = linkedSignal(() => this.initialPane());
  protected readonly fullscreen = signal(false);
  protected readonly timeZone = signal<ChartTimeZone>(persistedChartTimeZone());
  private readonly selectedIndicators = signal<readonly SelectedChartIndicator[]>([]);
  private readonly indicatorColorOverrides = signal<Readonly<Record<string, string>>>({});
  protected readonly polygonTimeframes = POLYGON_TIMEFRAMES;
  protected readonly liveResolutions = LIVE_RESOLUTIONS;
  private readonly indicatorCatalog = chartIndicatorCatalog();
  protected readonly indicatorCategories = this.indicatorCatalog.categories;
  protected readonly indicatorCatalogLoading = this.indicatorCatalog.loading;
  /** The shared catalog (`/api/dataset/available`) failed. The supported-set
   * failure is reported separately through `indicatorError`. */
  protected readonly indicatorCatalogLoadFailed = this.indicatorCatalog.loadFailed;

  /** The delayed pane is unavailable (#2211) either because the request was
   * rejected (`historyFailed`) or because it settled successfully with zero
   * bars and a notice outside the closed "genuinely empty" set
   * (`historyUnavailableNotice`). Either cause renders the same unavailable
   * state; only the message differs. */
  protected readonly historyUnavailable = computed(
    () => this.historyFailed() || this.historyUnavailableNotice() !== null,
  );

  /** The chart line's notice, when the backend says it has one to show. */
  protected readonly liveFeedNotice = computed<ChartFeedView | null>(() => {
    const feed = this.liveFeed();
    return feed?.show_notice ? feed : null;
  });
  protected readonly liveFeedDown = computed(() => this.liveFeed()?.attention_required === true);
  /** The traded asset's own bar line (#2915): it can stop while the chart's line draws. */
  protected readonly positionFeedNotice = computed<ChartFeedView | null>(() => {
    if (this.activePane() !== 'live') return null;
    const feed = this.livePosition()?.feed;
    return feed?.show_notice ? feed : null;
  });

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
  protected readonly activePosition = computed(() => {
    if (this.activePane() === 'live') return this.livePosition();
    return this.historyMatchesSelection() ? this.histPosition() : null;
  });
  protected readonly activeLoading = computed(() =>
    this.activePane() === 'live' ? this.liveLoading() : this.historyLoading(),
  );
  /** Fills on the candles and on the position pane, which draws the traded asset's (#2915). */
  protected readonly fillCount = computed(
    () => this.activeMarkers().length + (this.activePosition()?.fill_markers.length ?? 0),
  );
  protected readonly visibleFillCount = computed(() => {
    const bars = this.activeBars();
    const position = this.activePosition();
    const onPosition = position === null
      ? 0
      : position.kind === 'value' ? countPlottedFills(position.fill_markers, positionClock(position, bars))
      : positionRuns(position, bars).reduce((sum, run) => sum + countPlottedFills(position.fill_markers, run.bars), 0);
    return countPlottedFills(this.activeMarkers(), bars) + onPosition;
  });
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
        catchError((error: unknown) => of<IndicatorLoadState>({
          indicators: [],
          error: indicatorFailureMessage(error, 'for this candle window'),
          viewKey: params.viewKey,
        })),
      );
    },
  });
  protected readonly indicatorCalculationLoading = computed(() =>
    this.selectedIndicators().length > 0 && this.indicatorResource.isLoading(),
  );
  protected readonly indicatorError = computed(() => {
    if (this.indicatorCatalog.supportFailed()) {
      return 'The chart indicator catalog could not be loaded.';
    }
    return this.indicatorResource.hasValue() ? this.indicatorResource.value().error : null;
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
  /** Bumped when the visible bars or the chart's width change: lane marks move with the candles. */
  private readonly geometry = signal(0);
  private readonly plotWidth = signal(0);
  protected readonly placedLanes = computed((): readonly PlacedLane[] => {
    this.geometry();
    const bars = this.activeBars();
    const timeScale = this.chart?.timeScale();
    return placeLanes(
      this.lanes()?.(this.timeZone()) ?? [],
      bars.map((bar) => ({ startMs: bar.start_ms, closeMs: bar.end_ms })),
      timeScale === undefined ? undefined : barCoordinateScale(timeScale, bars),
      this.plotWidth(),
      'off',
    );
  });

  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Candlestick'> | null = null;
  private markers: ISeriesMarkersPluginApi<Time> | null = null;
  private indicatorSeries: ISeriesApi<SeriesType>[] = [];
  private renderedViewKey: string | null = null;
  private renderedBars: readonly ChartBar[] = [];

  constructor() {
    effect(() => this.renderActivePane());
    effect(() => {
      const selected = this.selectedIndicators();
      const loaded = this.indicatorResource.hasValue() ? this.indicatorResource.value() : null;
      if (selected.length === 0) {
        this.loadedIndicatorResults.set([]);
        this.loadedIndicatorViewKey.set(null);
      } else if (loaded?.error === null && loaded.viewKey === this.indicatorViewKey()) {
        this.loadedIndicatorResults.set(loaded.indicators);
        this.loadedIndicatorViewKey.set(loaded.viewKey);
      } else if (loaded?.error != null && loaded.viewKey === this.indicatorViewKey()) {
        // A failed calculation is said (below, beside the chart): lines from the last one are not left drawn.
        this.loadedIndicatorResults.set([]);
        this.loadedIndicatorViewKey.set(null);
      }
    });
    effect(() => this.renderIndicators(
      this.renderedIndicatorResults(),
      this.activeBars(),
      this.selectedIndicators(),
      this.indicatorColorOverrides(),
      this.activePosition(),
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
    const measure = () => this.measure();
    this.chart.timeScale().subscribeSizeChange(measure);
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(measure);
    this.applyTimeZoneFormatting();
    this.renderActivePane();
    this.renderIndicators(
      this.renderedIndicatorResults(),
      this.activeBars(),
      this.selectedIndicators(),
      this.indicatorColorOverrides(),
      this.activePosition(),
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
    try {
      globalThis.localStorage?.setItem(TIME_ZONE_STORAGE_KEY, timeZone);
    } catch {
      // Storage blocked: the choice applies for this visit only.
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

  private measure(): void {
    this.plotWidth.set(this.chart?.timeScale().width() ?? 0);
    this.geometry.update((tick) => tick + 1);
    const container = this.chartContainer().nativeElement;
    const origin = container.parentElement?.getBoundingClientRect().top ?? 0;
    const panes = this.chart?.panes?.() ?? [];
    this.premiumCaptionTops.set(Object.fromEntries(this.activePremiumPanes().flatMap((pane, index) => {
      const element = panes[this.premiumFirstPane + index]?.getHTMLElement();
      return element === null || element === undefined ? [] : [[pane.symbol, element.getBoundingClientRect().top - origin + 4]];
    })));
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

  /** The panes under the candles: each indicator's, then the position pane, which stays lowest. */
  private renderIndicators(
    results: readonly ChartIndicatorResult[],
    bars: readonly ChartBar[],
    selected: readonly SelectedChartIndicator[],
    colorOverrides: Readonly<Record<string, string>>,
    position: ChartPositionPane | null,
  ): void {
    if (!this.chart) return;
    for (const rendered of this.indicatorSeries) this.chart.removeSeries(rendered);
    this.indicatorSeries = [];

    const rules = this.activePane() === 'live' ? this.liveRuleLines() : this.histRuleLines();
    for (const rule of ruleLinePlans(rules, bars)) {
      const line = this.chart.addSeries(LineSeries, {
        color: rule.color, title: rule.title, lineWidth: 1, priceLineVisible: false,
        lastValueVisible: false, pointMarkersVisible: true,
      });
      line.setData(rule.points);
      this.indicatorSeries.push(line);
    }

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
    if (position !== null) this.renderPosition(position, bars, paneIndices.size + 1);
    this.renderPremiums(bars, paneIndices.size + (position === null ? 1 : 2));
  }

  private renderPremiums(bars: readonly ChartBar[], firstPane: number): void {
    const chart = this.chart;
    if (chart === null) return;
    this.premiumFirstPane = firstPane;
    premiumPanePlans(this.activePremiumPanes(), bars).forEach((plan, index) => {
      const pane = firstPane + index;
      const runs = plan.runs.length ? plan.runs : [{ points: [] }];
      for (const run of runs) {
        const line = chart.addSeries(LineSeries, {
          color: '#60a5fa', lineWidth: 2, title: plan.symbol, priceLineVisible: false,
          pointMarkersVisible: true,
        }, pane);
        line.setData([...run.points]);
        this.indicatorSeries.push(line);
      }
      for (const rule of plan.rules) {
        const line = chart.addSeries(LineSeries, {
          color: rule.color, title: rule.title, lineWidth: 1,
          lineStyle: rule.backtest ? 2 : 0,
          priceLineVisible: false, lastValueVisible: false, pointMarkersVisible: true,
        }, pane);
        line.setData(rule.points);
        this.indicatorSeries.push(line);
      }
      for (const run of plan.backtestRuns) {
        const line = chart.addSeries(LineSeries, {
          color: 'rgba(148, 163, 184, 0.4)', lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, pointMarkersVisible: true,
        }, pane);
        line.setData([...run.points]);
        this.indicatorSeries.push(line);
      }
      if (plan.fills.length) {
        plan.fillBounds.forEach((bounds, index) => {
          const anchors = chart.addSeries(LineSeries, { lineVisible: false, priceLineVisible: false, lastValueVisible: false }, pane);
          anchors.setData(bounds);
          if (index === 0) createSeriesMarkers(anchors, plan.fills);
          this.indicatorSeries.push(anchors);
        });
      }
    });
    requestAnimationFrame(() => { if (this.chart !== null) this.measure(); });
  }

  /**
   * One line per unbroken stretch of the traded asset's price, so a bar with
   * no price stays a gap. With no price at all one empty line still holds the
   * pane, so its caption never sits over another pane.
   */
  private renderPosition(position: ChartPositionPane, bars: readonly ChartBar[], paneIndex: number): void {
    const chart = this.chart;
    if (!chart) return;
    const runs = positionRuns(position, bars);
    const stretches: readonly PositionRun[] = runs.length > 0 ? runs : [{ points: [], bars: [] }];
    stretches.forEach((run, index) => {
      const line = chart.addSeries(LineSeries, {
        color: POSITION_LINE_COLOR,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: index === stretches.length - 1,
        crosshairMarkerVisible: false,
        pointMarkersVisible: position.kind === 'value',
      }, paneIndex);
      line.setData([...run.points]);
      if (position.kind === 'price') createSeriesMarkers(line, toSeriesMarkers(position.fill_markers, run.bars));
      this.indicatorSeries.push(line);
    });
    if (position.kind === 'value') {
      const clock = positionClock(position, bars);
      const anchor = chart.addSeries(LineSeries, { lineVisible: false, crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false }, paneIndex);
      anchor.setData(clock.map(bar => ({ time: Math.floor(bar.start_ms / 1000) as UTCTimestamp, value: 0 })));
      createSeriesMarkers(anchor, toSeriesMarkers(position.fill_markers, clock));
      this.indicatorSeries.push(anchor);
    }
  }
}
