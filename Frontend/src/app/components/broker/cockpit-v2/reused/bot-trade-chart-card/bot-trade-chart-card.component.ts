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
import type {
  ActiveDateEntry,
  ChartSnapshotResponse,
  ChartSnapshotRun,
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

// Hand-rolled formatters that use the browser's local TZ instead of UTC.
function formatLocalTickMark(time: UTCTimestamp, tickMarkType: number): string {
  const d = new Date((time as number) * 1000);
  const pad = (n: number) => n.toString().padStart(2, '0');
  switch (tickMarkType) {
    case 0:
      return d.getFullYear().toString();
    case 1:
      return d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
    case 2:
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    case 3:
      return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    case 4:
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    default:
      return '';
  }
}

export function localDateString(now = new Date()): string {
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-');
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

/** Map a strategy/fill event timestamp onto the candle timestamp used by
 * lightweight-charts. Broker bars are keyed by ``start_ms`` on the chart,
 * while the live engine records trade rows at the bar close/fill instant.
 * Markers whose time is not present in the series can disappear, so snap
 * events to the candle whose [start_ms, end_ms] contains them. */
export function markerTimeForEventMs(
  eventMs: number,
  bars: readonly IbkrMinuteBar[],
): UTCTimestamp {
  const bar = bars.find((b) => b.start_ms <= eventMs && eventMs <= b.end_ms);
  return ((bar?.start_ms ?? eventMs) / 1000) as UTCTimestamp;
}

export function filterActivityItemsForSymbol<T extends { symbol: string }>(
  activitySymbol: string,
  items: T[],
): T[] {
  const chartSymbol = activitySymbol.trim().toUpperCase();
  if (!chartSymbol) return items;
  return items.filter((item) => item.symbol.toUpperCase() === chartSymbol);
}

export type ChartResolution = '1m' | '5s';

export interface ChartSelection {
  readonly sessionDate: string;
  readonly resolution: ChartResolution;
}

// Per-run color palette. Old runs darker, current run is the green pulse
// used elsewhere on the panel — keeps the eye drawn to the live session.
const RUN_COLORS = ['#60a5fa', '#a78bfa', '#f472b6', '#fbbf24', '#fb923c'];

const RESOLUTION_META: Record<
  ChartResolution,
  {
    secondsVisible: boolean;
    label: string;
    subtitle: string;
  }
> = {
  '1m': {
    secondsVisible: false,
    label: '1m',
    subtitle: "1-minute candles from the broker feed; markers show the bot's trades.",
  },
  '5s': {
    secondsVisible: true,
    label: '5s',
    subtitle:
      'Raw 5-second candles streamed from Interactive Brokers; same trade markers as the 1-min view.',
  },
};

const CHART_HEIGHT_PX = 380;
const RESOLUTION_OPTIONS: ChartResolution[] = ['1m', '5s'];

@Component({
  selector: 'app-bot-trade-chart-card',
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
  /** Initial bar resolution shown when the card mounts. */
  readonly initialResolution = input<ChartResolution>('1m');
  readonly selectionChange = output<ChartSelection>();

  /** The date currently being displayed. Owned by the card's own selector
   * (Slice 6) — defaults to today and stops live polling when the user
   * picks a past date. */
  protected readonly chartDate = signal<string>(localDateString());
  protected readonly todayDate = signal<string>(localDateString());

  protected readonly resolution = signal<ChartResolution>('1m');
  protected readonly resolutionOptions = RESOLUTION_OPTIONS;
  protected readonly meta = computed(() => RESOLUTION_META[this.resolution()]);
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

  protected readonly snapshotResource = resource<
    ChartSnapshotResponse | null,
    { instanceId: string | null; date: string | null; resolution: ChartResolution }
  >({
    params: () => ({
      instanceId: this.strategyInstanceId(),
      date: this.chartDate(),
      resolution: this.resolution(),
    }),
    loader: ({ params }) => this.loadSnapshot(params.instanceId, params.date, params.resolution),
  });

  protected readonly bars = computed<IbkrMinuteBar[]>(
    () => this.activity()?.bars ?? this.snapshotResource.value()?.bars ?? [],
  );

  protected readonly runs = computed<ChartSnapshotRun[]>(
    () => this.snapshotResource.value()?.runs ?? [],
  );

  /** Date picker source (Slice 6) — every date the instance has touched.
   * The picker shows ``has_bars=false`` dates greyed out so the operator
   * sees them but knows the chart will surface a "bars unavailable" badge. */
  protected readonly activeDatesResource = resource<
    ActiveDateEntry[],
    string | null
  >({
    params: () => this.strategyInstanceId(),
    loader: ({ params }) => this.loadActiveDates(params),
  });

  protected readonly activeDates = computed<ActiveDateEntry[]>(
    () => this.activeDatesResource.value() ?? [],
  );
  protected readonly pickerDates = computed<ActiveDateEntry[]>(() =>
    this.activeDates().filter((d) => d.date !== this.todayDate()),
  );

  /** True for the currently-displayed date when persistence has no bars for
   * it — the "bars unavailable" badge case (Slice 6). */
  protected readonly missingBarsForSelectedDate = computed<boolean>(() => {
    const date = this.chartDate();
    const entry = this.activeDates().find((d) => d.date === date);
    return entry !== undefined && !entry.has_bars;
  });

  protected readonly isPastDate = computed<boolean>(() => {
    return this.chartDate() < this.todayDate();
  });

  protected readonly statusLabel = computed<string>(() => {
    if (this.strategyInstanceId() === null) return 'No instance selected';
    if (this.symbol() === null) return 'No symbol resolved yet';
    const snap = this.snapshotResource.value();
    const activity = this.activity();
    if (activity) {
      return activity.has_bars ? `Session ${activity.session_date}` : 'Bars unavailable for this date';
    }
    if (!snap) return this.snapshotResource.isLoading() ? 'Loading…' : '';
    if (this.isPastDate()) {
      return snap.has_bars ? `Static — ${snap.date}` : 'Bars unavailable for this date';
    }
    return snap.has_bars ? 'Streaming' : 'Waiting for first bar…';
  });

  protected readonly statusTone = computed<'ok' | 'warn' | 'bad' | 'idle'>(() => {
    const snap = this.snapshotResource.value();
    const activity = this.activity();
    if (activity) return activity.has_bars ? 'ok' : 'warn';
    if (snap === null || snap === undefined) {
      return this.snapshotResource.isLoading() ? 'warn' : 'idle';
    }
    if (this.isPastDate()) return snap.has_bars ? 'ok' : 'warn';
    return snap.has_bars ? 'ok' : 'warn';
  });

  protected readonly tradeCount = computed<number>(() =>
    this.activityFillMarkers()?.length
      ?? this.runs().reduce((sum, run) => sum + run.trades.length, 0),
  );

  protected readonly activityFillMarkers = computed(() => {
    const activity = this.activity();
    if (!activity) return null;
    return filterActivityItemsForSymbol(activity.symbol, activity.fill_markers);
  });

  protected readonly activityPositionAnnotations = computed(() => {
    const activity = this.activity();
    if (!activity) return null;
    return filterActivityItemsForSymbol(activity.symbol, activity.position_annotations);
  });

  constructor() {
    effect(() => {
      this.resolution.set(this.initialResolution());
    });

    effect(() => {
      this.selectionChange.emit({
        sessionDate: this.chartDate(),
        resolution: this.resolution(),
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
      const r = this.resolution();
      if (this.chart) {
        this.chart.applyOptions({
          timeScale: { secondsVisible: RESOLUTION_META[r].secondsVisible },
        });
      }
      this.liveAtEdge.set(true);
    });

    // Live polling only on today's view; past dates are static.
    this.pollTimer = setInterval(() => {
      const priorToday = this.todayDate();
      const nextToday = localDateString();
      if (nextToday !== priorToday) {
        this.todayDate.set(nextToday);
        if (this.chartDate() === priorToday) {
          this.chartDate.set(nextToday);
        }
      }
      if (!this.isPastDate()) {
        this.snapshotResource.reload();
      }
    }, POLL_INTERVAL_MS);

    this.destroyRef.onDestroy(() => {
      if (this.pollTimer !== null) clearInterval(this.pollTimer);
      this.resizeObserver?.disconnect();
      this.chart?.remove();
      this.chart = null;
    });
  }

  protected setResolution(next: ChartResolution): void {
    if (this.resolution() === next) return;
    this.resolution.set(next);
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
    date: string | null,
    resolution: ChartResolution,
  ): Promise<ChartSnapshotResponse | null> {
    if (!instanceId) return null;
    const params: Record<string, string> = { resolution };
    if (date) params['date'] = date;
    return firstValueFrom(
      this.http.get<ChartSnapshotResponse>(
        `/api/live-instances/${encodeURIComponent(instanceId)}/chart-snapshot`,
        { params },
      ),
    );
  }

  private async loadActiveDates(instanceId: string | null): Promise<ActiveDateEntry[]> {
    if (!instanceId) return [];
    return firstValueFrom(
      this.http.get<ActiveDateEntry[]>(
        `/api/live-instances/${encodeURIComponent(instanceId)}/active-dates`,
      ),
    );
  }

  /** Date-picker change handler (Slice 6). Setting a date triggers the
   * snapshot resource to refetch via its params dependency. */
  protected onDateSelected(date: string): void {
    this.chartDate.set(date);
  }

  private initChart(): void {
    const el = this.container().nativeElement;
    const m = this.meta();
    const crosshairTimeFormatter = (time: Time): string => {
      const d = new Date((time as number) * 1000);
      return this.meta().secondsVisible
        ? d.toLocaleTimeString(undefined, { hour12: false })
        : d.toLocaleTimeString(undefined, {
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
      timeScale: {
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
    const bars = this.bars();
    this.candles.setData(
      bars.map((b) => ({
        time: (b.start_ms / 1000) as UTCTimestamp,
        open: Number(b.open),
        high: Number(b.high),
        low: Number(b.low),
        close: Number(b.close),
      })),
    );
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
          time: markerTimeForEventMs(marker.exec_ts_ms, bars),
          position: isBuy ? 'belowBar' : 'aboveBar',
          color: isBuy ? '#60a5fa' : '#f97316',
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          text:
            `${marker.side} ${marker.quantity}` +
            ` · ${marker.position_effect}` +
            (marker.replay_count > 1 ? ` · seen ${marker.replay_count}x` : ''),
        });
      });
      this.activityPositionAnnotations()?.forEach((annotation) => {
        out.push({
          time: markerTimeForEventMs(annotation.ts_ms, bars),
          position: annotation.label === 'OPEN' ? 'belowBar' : 'aboveBar',
          color: annotation.uncertain ? '#fbbf24' : '#e2e8f0',
          shape: 'circle',
          text: annotation.uncertain ? 'POSITION UNCERTAIN' : annotation.label,
        });
      });
      out.sort((a, b) => (a.time as number) - (b.time as number));
      this.markersPlugin.setMarkers(out);
      return;
    }
    this.runs().forEach((run) => {
      const color = this.runColor(run);
      run.trades.forEach((t, i) => {
        out.push({
          time: markerTimeForEventMs(t.entry_time_ms, bars),
          position: 'belowBar',
          color,
          shape: 'arrowUp',
          text: `BUY #${i + 1}`,
        });
        out.push({
          time: markerTimeForEventMs(t.exit_time_ms, bars),
          position: 'aboveBar',
          color: t.pnl_points >= 0 ? '#4ade80' : '#ef4444',
          shape: 'circle',
          text: `CLOSE ${t.pnl_points >= 0 ? '+' : ''}${t.pnl_points.toFixed(2)}`,
        });
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
