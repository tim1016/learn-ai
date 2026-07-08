import { HttpClient } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import {
  type CandlestickData,
  CandlestickSeries,
  CrosshairMode,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LogicalRange,
  LineStyle,
  type PriceLineOptions,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';
import { firstValueFrom } from 'rxjs';
import { AssetIdentityComponent } from '../../../../../shared/asset-identity';
import type {
  ActivityFillMarker,
  ChartBaseResolution,
  ChartSnapshotResponse,
  ChartSnapshotRun,
  ChartTimeframe,
  IbkrMinuteBar,
  LiveInstanceActivityProjection,
} from './bot-trade-chart-card.types';

const POLL_INTERVAL_MS = 5_000;

// Distance (in logical-index units) from the last bar within which the chart
// is considered "at the live edge". The visible range's ``to`` is fractional
// and floats slightly past the last index even when the user has not scrolled;
// ½ a bar is the smallest threshold that doesn't flicker the LIVE pill on
// every new bar emit but still flips it off as soon as the user pans back
// a couple of bars to inspect history.
const LIVE_EDGE_THRESHOLD_BARS = 0.5;
const LOCAL_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

function formatLocalDate(ms: number, options: Intl.DateTimeFormatOptions): string {
  return new Date(ms).toLocaleString(undefined, {
    ...options,
    timeZone: LOCAL_TIME_ZONE,
  });
}

// Hand-rolled formatters that use the browser's local TZ instead of UTC.
function formatLocalTickMark(time: UTCTimestamp, tickMarkType: number): string {
  const ms = (time as number) * 1000;
  switch (tickMarkType) {
    case 0:
      return formatLocalDate(ms, { year: 'numeric' });
    case 1:
      return formatLocalDate(ms, { month: 'short', year: 'numeric' });
    case 2:
      return formatLocalDate(ms, { month: 'short', day: 'numeric' });
    case 3:
      return formatLocalDate(ms, { hour: '2-digit', minute: '2-digit', hour12: false });
    case 4:
      return formatLocalDate(ms, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    default:
      return formatLocalDate(ms, { hour: '2-digit', minute: '2-digit', hour12: false });
  }
}

export function localDateString(now = new Date()): string {
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-');
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function localDateParts(value: string): readonly [number, number, number] {
  const [year, month, day] = value.split('-').map(Number);
  return [year, month - 1, day];
}

export function localDateStartMs(value: string): number {
  return new Date(...localDateParts(value)).getTime();
}

export function localDateEndMs(value: string): number {
  const [year, month, day] = localDateParts(value);
  return new Date(year, month, day + 1).getTime();
}

function clampDateString(value: string, min: string, max: string): string {
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

/** True when the chart's right-edge is showing the latest bar — i.e. the
 * user has not scrolled back to inspect history. */
export function isAtLiveEdge(
  range: LogicalRange | null,
  barCount: number,
  threshold = LIVE_EDGE_THRESHOLD_BARS,
): boolean {
  if (range === null) return true;
  if (barCount === 0) return true;
  return range.to >= barCount - 1 - threshold;
}

export function visibleRangeToRestore(
  liveAtEdge: boolean,
  range: LogicalRange | null,
): LogicalRange | null {
  return liveAtEdge ? null : range;
}

/** Map a strategy/fill event timestamp onto the candle timestamp used by
 * lightweight-charts. Broker bars are keyed by ``start_ms`` on the chart,
 * while the live engine records trade rows at the bar close/fill instant.
 * Markers whose time is not present in the series can disappear, so snap
 * events to the candle whose [start_ms, end_ms] contains them. */
export function markerTimeForEventMs(
  eventMs: number,
  bars: readonly IbkrMinuteBar[],
): UTCTimestamp {
  if (bars.length === 0) return (eventMs / 1000) as UTCTimestamp;
  let nearest = bars[0];
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (const bar of bars) {
    if (bar.start_ms <= eventMs && eventMs < bar.end_ms) {
      return (bar.start_ms / 1000) as UTCTimestamp;
    }
    const distance =
      eventMs < bar.start_ms ? bar.start_ms - eventMs : eventMs - bar.end_ms;
    if (distance < nearestDistance) {
      nearest = bar;
      nearestDistance = distance;
    }
  }
  return (nearest.start_ms / 1000) as UTCTimestamp;
}

export function filterActivityItemsForSymbol<T extends { symbol: string }>(
  activitySymbol: string,
  items: T[],
): T[] {
  const chartSymbol = activitySymbol.trim().toUpperCase();
  if (!chartSymbol) return items;
  return items.filter((item) => item.symbol.toUpperCase() === chartSymbol);
}

export function markerTimeForActivityFill(
  marker: ActivityFillMarker,
  bars: readonly IbkrMinuteBar[],
): UTCTimestamp {
  return markerTimeForEventMs(marker.chart_ts_ms, bars);
}

function sourceSignature(source: IbkrMinuteBar['source']): number {
  switch (source) {
    case 'polygon':
      return 2;
    case 'mixed':
      return 3;
    default:
      return 1;
  }
}

function priceSignature(value: string): number {
  return Math.round(Number(value) * 10_000);
}

export function candleSignatureForBars(bars: readonly IbkrMinuteBar[]): string {
  let hash = 2_166_136_261;
  for (const bar of bars) {
    hash = Math.imul(hash ^ bar.start_ms, 16_777_619);
    hash = Math.imul(hash ^ bar.end_ms, 16_777_619);
    hash = Math.imul(hash ^ priceSignature(bar.open), 16_777_619);
    hash = Math.imul(hash ^ priceSignature(bar.high), 16_777_619);
    hash = Math.imul(hash ^ priceSignature(bar.low), 16_777_619);
    hash = Math.imul(hash ^ priceSignature(bar.close), 16_777_619);
    hash = Math.imul(hash ^ sourceSignature(bar.source), 16_777_619);
  }
  return `${bars.length}:${hash >>> 0}`;
}

export function candleDataForBar(bar: IbkrMinuteBar): CandlestickData<UTCTimestamp> {
  const open = Number(bar.open);
  const close = Number(bar.close);
  const base = {
    time: (bar.start_ms / 1000) as UTCTimestamp,
    open,
    high: Number(bar.high),
    low: Number(bar.low),
    close,
  };
  if (bar.source === 'polygon') {
    return {
      ...base,
      color: 'rgba(148, 163, 184, 0.24)',
      borderColor: 'rgba(203, 213, 225, 0.72)',
      wickColor: 'rgba(203, 213, 225, 0.58)',
    };
  }
  if (bar.source === 'mixed') {
    return {
      ...base,
      borderColor: '#facc15',
      wickColor: '#facc15',
    };
  }
  return base;
}

export function isSnapshotTradeCoveredByActivity(
  eventMs: number,
  activity: Pick<LiveInstanceActivityProjection, 'session_date'> | null,
): boolean {
  return activity !== null && localDateString(new Date(eventMs)) === activity.session_date;
}

export interface ChartSelection {
  readonly sessionDate: string;
  readonly timeframe: ChartTimeframe;
  readonly activityResolution: ChartBaseResolution;
  readonly fromMs: number;
  readonly toMs: number;
}

// Per-run color palette. Old runs darker, current run is the green pulse
// used elsewhere on the panel — keeps the eye drawn to the live session.
const RUN_COLORS = ['#60a5fa', '#a78bfa', '#f472b6', '#fbbf24', '#fb923c'];

const TIMEFRAME_META: Record<
  ChartTimeframe,
  {
    secondsVisible: boolean;
    label: string;
    subtitle: string;
  }
> = {
  '1m': {
    secondsVisible: false,
    label: '1m',
    subtitle: "1-minute candles from the broker feed with Polygon overlay for missing history.",
  },
  '5m': {
    secondsVisible: false,
    label: '5m',
    subtitle: '5-minute candles aggregated from the 1-minute chart base.',
  },
  '15m': {
    secondsVisible: false,
    label: '15m',
    subtitle: '15-minute candles aggregated from the 1-minute chart base.',
  },
  '1h': {
    secondsVisible: false,
    label: '1h',
    subtitle: 'Hourly candles aggregated from the 1-minute chart base.',
  },
  '1d': {
    secondsVisible: false,
    label: '1d',
    subtitle: 'Daily candles aggregated from regular-session 1-minute bars.',
  },
  '5s': {
    secondsVisible: true,
    label: '5s',
    subtitle:
      'Raw 5-second candles streamed from Interactive Brokers; same trade markers as the 1-min view.',
  },
};

const CHART_HEIGHT_PX = 380;
const TIMEFRAME_OPTIONS: ChartTimeframe[] = ['5s', '1m', '5m', '15m', '1h', '1d'];

interface SnapshotCache {
  readonly key: string;
  readonly snapshot: ChartSnapshotResponse;
}

@Component({
  selector: 'app-bot-trade-chart-card',
  imports: [AssetIdentityComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-trade-chart-card.component.html',
  styleUrl: './bot-trade-chart-card.component.scss',
})
export class BotTradeChartCardComponent {
  /** Strategy instance id — drives the `/chart-snapshot` URL. The chart card
   * is now instance-addressed (Slice 5): the parent passes the instance,
   * not a single run, so per-run color tags and inactive-interval shading
   * can render every run that touched the day. ``null`` = no instance
   * selected; the card sits idle. */
  readonly strategyInstanceId = input<string | null>(null);
  /** The traded symbol, sourced by the parent from
   * ``LiveInstanceStatus.symbol`` (Slice 2). The chart card no longer
   * fetches its own bars by symbol — the server resolves it from the
   * ledger — but the symbol shows up in the status chip. ``null`` =
   * unknown — the chart renders an idle state. */
  readonly symbol = input<string | null>(null);
  /** Materialized Activity projection. When present, chart markers come from
   * broker-confirmed fills in this backend-owned ledger instead of from
   * trades.parquet. */
  readonly activity = input<LiveInstanceActivityProjection | null>(null);
  /** Initial chart timeframe shown when the card mounts. */
  readonly initialTimeframe = input<ChartTimeframe>('1m');
  readonly selectionChange = output<ChartSelection>();

  protected readonly todayDate = signal<string>(localDateString());
  protected readonly minRangeDate = computed<string>(() =>
    localDateString(addDays(new Date(`${this.todayDate()}T00:00:00`), -6)),
  );
  protected readonly rangeStartDate = signal<string>(localDateString(addDays(new Date(), -6)));
  protected readonly rangeEndDate = signal<string>(localDateString());
  private readonly nowMs = signal<number>(Date.now());

  protected readonly timeframe = signal<ChartTimeframe>('1m');
  protected readonly timeframeOptions = TIMEFRAME_OPTIONS;
  protected readonly meta = computed(() => TIMEFRAME_META[this.timeframe()]);
  protected readonly chartHeightPx = CHART_HEIGHT_PX;

  /** False once the user has scrolled back from the live edge; the LIVE pill
   * dims when this is false and a click on it scrolls back to real-time. */
  protected readonly liveAtEdge = signal<boolean>(true);

  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly container =
    viewChild.required<ElementRef<HTMLDivElement>>('container');

  private chart: IChartApi | null = null;
  private candles: ISeriesApi<'Candlestick'> | null = null;
  private markersPlugin: ISeriesMarkersPluginApi<Time> | null = null;
  private activeLine: IPriceLine | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private lastCandleSignature = '';

  protected readonly windowFromMs = computed<number>(() => localDateStartMs(this.rangeStartDate()));
  protected readonly windowToMs = computed<number>(() => {
    if (this.rangeEndDate() >= this.todayDate()) return this.nowMs();
    return localDateEndMs(this.rangeEndDate());
  });
  protected readonly selectedSessionDate = computed<string>(() => this.rangeEndDate());
  protected readonly isLiveRange = computed<boolean>(() => this.rangeEndDate() >= this.todayDate());
  protected readonly activityResolution = computed<ChartBaseResolution>(() =>
    this.timeframe() === '5s' ? '5s' : '1m',
  );
  protected readonly localTimeZone = LOCAL_TIME_ZONE;
  private readonly snapshotCacheKey = computed<string>(() =>
    [
      this.strategyInstanceId() ?? 'none',
      this.rangeStartDate(),
      this.rangeEndDate(),
      this.timeframe(),
    ].join(':'),
  );
  private readonly snapshotCache = signal<SnapshotCache | null>(null);

  protected readonly snapshotResource = resource<
    ChartSnapshotResponse | null,
    {
      instanceId: string | null;
      fromMs: number;
      toMs: number;
      timeframe: ChartTimeframe;
    }
  >({
    params: () => ({
      instanceId: this.strategyInstanceId(),
      fromMs: this.windowFromMs(),
      toMs: this.windowToMs(),
      timeframe: this.timeframe(),
    }),
    loader: ({ params }) =>
      this.loadSnapshot(params.instanceId, params.fromMs, params.toMs, params.timeframe),
  });

  private readonly snapshot = computed<ChartSnapshotResponse | null>(() => {
    const current = this.snapshotResource.value();
    if (current) return current;
    const cached = this.snapshotCache();
    return cached?.key === this.snapshotCacheKey() ? cached.snapshot : null;
  });

  protected readonly bars = computed<IbkrMinuteBar[]>(
    () => this.snapshot()?.bars ?? [],
  );

  protected readonly runs = computed<ChartSnapshotRun[]>(
    () => this.snapshot()?.runs ?? [],
  );

  protected readonly overlayBarCount = computed<number>(() =>
    this.bars().filter((bar) => bar.source === 'polygon').length,
  );
  protected readonly mixedBarCount = computed<number>(() =>
    this.bars().filter((bar) => bar.source === 'mixed').length,
  );
  protected readonly overlayNotices = computed(() => this.snapshot()?.overlay_notices ?? []);
  protected readonly liveStreaming = computed<boolean>(() => this.snapshot()?.is_streaming ?? false);

  protected readonly statusLabel = computed<string>(() => {
    if (this.strategyInstanceId() === null) return 'No instance selected';
    if (this.symbol() === null) return 'No symbol resolved yet';
    const snap = this.snapshot();
    if (!snap) return this.snapshotResource.isLoading() ? 'Loading…' : '';
    if (!snap.has_bars) return 'No bars in selected range';
    if (snap.is_streaming) return 'Streaming';
    return `${this.rangeStartDate()} to ${this.rangeEndDate()}`;
  });

  protected readonly statusTone = computed<'ok' | 'warn' | 'bad' | 'idle'>(() => {
    const snap = this.snapshot();
    if (snap === null || snap === undefined) {
      return this.snapshotResource.isLoading() ? 'warn' : 'idle';
    }
    if (!snap.has_bars) return 'warn';
    return snap.is_streaming || this.overlayNotices().length === 0 ? 'ok' : 'warn';
  });

  protected readonly tradeCount = computed<number>(() =>
    this.activityFillMarkers()?.length
      ?? this.runs().reduce((sum, run) => sum + run.trades.length, 0),
  );

  protected readonly activityFillMarkers = computed(() => {
    const activity = this.activity();
    if (!activity) return null;
    return filterActivityItemsForSymbol(activity.symbol, activity.fill_markers)
      .filter((marker) => this.windowFromMs() <= marker.chart_ts_ms && marker.chart_ts_ms < this.windowToMs());
  });

  constructor() {
    effect(() => {
      this.timeframe.set(this.initialTimeframe());
    });

    effect(() => {
      const snap = this.snapshotResource.value();
      if (snap) {
        this.snapshotCache.set({
          key: this.snapshotCacheKey(),
          snapshot: snap,
        });
      }
    });

    effect(() => {
      this.selectionChange.emit({
        sessionDate: this.selectedSessionDate(),
        timeframe: this.timeframe(),
        activityResolution: this.activityResolution(),
        fromMs: this.windowFromMs(),
        toMs: this.windowToMs(),
      });
    });

    afterNextRender(() => this.initChart());

    effect(() => {
      this.bars();
      if (this.chart) this.syncCandles();
    });
    effect(() => {
      this.runs();
      this.bars();
      if (this.chart) this.syncMarkers();
    });
    effect(() => {
      this.runs();
      if (this.chart) this.syncActiveLine();
    });

    // Rebuild the chart's seconds-axis when the toggle flips.
    effect(() => {
      const r = this.timeframe();
      if (this.chart) {
        this.chart.applyOptions({
          timeScale: { secondsVisible: TIMEFRAME_META[r].secondsVisible },
        });
      }
      this.liveAtEdge.set(true);
    });

    // Live polling only when the range ends today; past ranges are static.
    this.pollTimer = setInterval(() => {
      this.nowMs.set(Date.now());
      const priorToday = this.todayDate();
      const nextToday = localDateString();
      if (nextToday !== priorToday) {
        this.todayDate.set(nextToday);
        if (this.rangeEndDate() === priorToday) {
          this.rangeEndDate.set(nextToday);
        }
        this.rangeStartDate.set(clampDateString(this.rangeStartDate(), this.minRangeDate(), nextToday));
      }
    }, POLL_INTERVAL_MS);

    this.destroyRef.onDestroy(() => {
      if (this.pollTimer !== null) clearInterval(this.pollTimer);
      this.resizeObserver?.disconnect();
      this.chart?.remove();
      this.chart = null;
    });
  }

  protected setTimeframe(next: ChartTimeframe): void {
    if (this.timeframe() === next) return;
    this.timeframe.set(next);
  }

  /** Click handler on the LIVE pill — scrolls the chart back to real-time. */
  protected resumeLive(): void {
    this.chart?.timeScale().scrollToRealTime();
    this.liveAtEdge.set(true);
  }

  /** Color the frontend assigns to a given run for marker tagging. */
  protected runColor(run: ChartSnapshotRun): string {
    return RUN_COLORS[run.color_index % RUN_COLORS.length];
  }

  private async loadSnapshot(
    instanceId: string | null,
    fromMs: number,
    toMs: number,
    timeframe: ChartTimeframe,
  ): Promise<ChartSnapshotResponse | null> {
    if (!instanceId) return null;
    const params: Record<string, string> = {
      from_ms: String(fromMs),
      to_ms: String(toMs),
      timeframe,
    };
    return firstValueFrom(
      this.http.get<ChartSnapshotResponse>(
        `/api/live-instances/${encodeURIComponent(instanceId)}/chart-snapshot`,
        { params },
      ),
    );
  }

  protected onRangeStartSelected(date: string): void {
    const clamped = clampDateString(date, this.minRangeDate(), this.rangeEndDate());
    this.liveAtEdge.set(clamped === this.minRangeDate() && this.rangeEndDate() === this.todayDate());
    this.rangeStartDate.set(clamped);
  }

  protected onRangeEndSelected(date: string): void {
    const max = this.todayDate();
    const clamped = clampDateString(date, this.rangeStartDate(), max);
    this.liveAtEdge.set(true);
    this.rangeEndDate.set(clamped);
  }

  protected jumpToLiveRange(): void {
    this.rangeEndDate.set(this.todayDate());
    this.rangeStartDate.set(this.minRangeDate());
    this.resumeLive();
  }

  private initChart(): void {
    const el = this.container().nativeElement;
    const m = this.meta();
    const crosshairTimeFormatter = (time: Time): string => {
      return this.meta().secondsVisible
        ? formatLocalDate((time as number) * 1000, { hour12: false })
        : formatLocalDate((time as number) * 1000, {
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          });
    };
    this.chart = createChart(el, {
      width: el.clientWidth,
      height: CHART_HEIGHT_PX,
      layout: {
        background: { color: 'transparent' },
        textColor: '#cbd5e1',
        attributionLogo: false,
      },
      localization: {
        timeFormatter: crosshairTimeFormatter,
      },
      grid: {
        vertLines: { color: 'rgba(148, 163, 184, 0.08)' },
        horzLines: { color: 'rgba(148, 163, 184, 0.08)' },
      },
      rightPriceScale: { borderColor: 'rgba(148, 163, 184, 0.2)' },
      handleScroll: {
        mouseWheel: false,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: false,
        mouseWheel: false,
        pinch: false,
      },
      timeScale: {
        visible: true,
        timeVisible: true,
        secondsVisible: m.secondsVisible,
        borderColor: 'rgba(148, 163, 184, 0.2)',
        tickMarkFormatter: formatLocalTickMark,
        rightOffset: 2,
        shiftVisibleRangeOnNewBar: true,
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
    this.markersPlugin = createSeriesMarkers(this.candles, []);

    this.syncCandles();
    this.syncMarkers();
    this.syncActiveLine();

    this.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      this.liveAtEdge.set(isAtLiveEdge(range, this.bars().length));
    });

    this.resizeObserver = new ResizeObserver((entries) => {
      if (!this.chart || !entries.length) return;
      const { width } = entries[0].contentRect;
      this.chart.applyOptions({ width });
    });
    this.resizeObserver.observe(el);
  }

  private syncCandles(): void {
    if (!this.candles) return;
    const timeScale = this.chart?.timeScale();
    const restoreRange = visibleRangeToRestore(
      this.liveAtEdge(),
      timeScale?.getVisibleLogicalRange() ?? null,
    );
    const bars = this.bars();
    const signature = candleSignatureForBars(bars);
    if (signature === this.lastCandleSignature) return;
    this.lastCandleSignature = signature;
    this.candles.setData(bars.map(candleDataForBar));
    if (restoreRange !== null) {
      timeScale?.setVisibleLogicalRange(restoreRange);
    }
  }

  /** Render trade markers across every run that touched the day, with a
   * per-run color tag so the eye can tell a fresh bot session from yesterday's
   * leftover trades on the same chart. */
  private syncMarkers(): void {
    if (!this.markersPlugin) return;
    const out: SeriesMarker<Time>[] = [];
    const bars = this.bars();
    const activity = this.activity();
    if (activity) {
      this.activityFillMarkers()?.forEach((marker) => {
        const isBuy = marker.side === 'BUY';
        out.push({
          time: markerTimeForActivityFill(marker, bars),
          position: isBuy ? 'belowBar' : 'aboveBar',
          color: isBuy ? '#60a5fa' : '#f97316',
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          text:
            `${marker.side} ${marker.quantity}` +
            ` · ${marker.position_effect}` +
            (marker.replay_count > 1 ? ` · seen ${marker.replay_count}x` : ''),
        });
      });
    }
    this.runs().forEach((run) => {
      const color = this.runColor(run);
      run.trades.forEach((t, i) => {
        if (!isSnapshotTradeCoveredByActivity(t.entry_time_ms, activity)) {
          out.push({
            time: markerTimeForEventMs(t.entry_time_ms, bars),
            position: 'belowBar',
            color,
            shape: 'arrowUp',
            text: `BUY #${i + 1}`,
          });
        }
        if (!isSnapshotTradeCoveredByActivity(t.exit_time_ms, activity)) {
          out.push({
            time: markerTimeForEventMs(t.exit_time_ms, bars),
            position: 'aboveBar',
            color: t.pnl_points >= 0 ? '#4ade80' : '#ef4444',
            shape: 'circle',
            text: `CLOSE ${t.pnl_points >= 0 ? '+' : ''}${t.pnl_points.toFixed(2)}`,
          });
        }
      });
    });
    out.sort((a, b) => (a.time as number) - (b.time as number));
    this.markersPlugin.setMarkers(out);
  }

  /** Active-entry line: scoped to the current (is_current) run only — an
   * exited prior run's last fill is not "active". */
  private syncActiveLine(): void {
    if (!this.candles) return;
    if (this.activeLine) {
      this.candles.removePriceLine(this.activeLine);
      this.activeLine = null;
    }
    const current = this.runs().find((r) => r.is_current);
    if (!current || current.executions.length === 0) return;
    const trades = current.trades;
    const lastExit = trades.length > 0 ? trades[trades.length - 1].exit_time_ms : 0;
    const lastExec = current.executions[current.executions.length - 1];
    if (lastExec.ts_ms <= lastExit) return;

    const opts: PriceLineOptions = {
      price: lastExec.fill_price,
      color: '#fbbf24',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: `ENTRY ${lastExec.fill_price.toFixed(2)}`,
      lineVisible: true,
      axisLabelColor: '#0f172a',
      axisLabelTextColor: '#fbbf24',
    };
    this.activeLine = this.candles.createPriceLine(opts);
  }
}
