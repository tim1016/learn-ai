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
  ExecutionRow,
  IbkrBarsSnapshot,
  IbkrMinuteBar,
  TradeRow,
} from './bot-trade-chart-card.types';

const POLL_INTERVAL_MS = 5_000;

// Hand-rolled formatters that use the browser's local TZ instead of UTC.
// Lightweight-charts hands us a UTCTimestamp (seconds since epoch) and a
// tickMarkType (0=Year, 1=Month, 2=DayOfMonth, 3=Time, 4=TimeWithSeconds);
// the shared ``chart-utils.formatTickMark`` formats those as UTC via
// ``getUTCHours``/``getUTCMinutes`` — kept that way because replay-chart-v2
// and other consumers display canonical UTC. Live broker bars on the
// operator panel are interpreted in the operator's local time instead;
// that's what the operator's wall clock shows, and it matches the NY
// time used elsewhere on the panel (broker is in NY hours during EDT).
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

export type ChartResolution = '1m' | '5s';

const RESOLUTION_META: Record<
  ChartResolution,
  {
    endpoint: string;
    title: string;
    subtitle: string;
    secondsVisible: boolean;
    chartHeight: number;
  }
> = {
  '1m': {
    endpoint: '/api/broker/bars/snapshot',
    title: 'Price & Trades — 1-min',
    subtitle: "1-minute candles from the broker feed; markers show the bot's trades.",
    secondsVisible: false,
    chartHeight: 380,
  },
  '5s': {
    endpoint: '/api/broker/bars-5s/snapshot',
    title: 'Price & Trades — 5-sec',
    subtitle: 'Raw 5-second candles streamed from Interactive Brokers; same trade markers as the 1-min view.',
    secondsVisible: true,
    chartHeight: 300,
  },
};

@Component({
  selector: 'app-bot-trade-chart-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-trade-chart-card.component.html',
  styleUrl: './bot-trade-chart-card.component.scss',
})
export class BotTradeChartCardComponent {
  /** The run whose trades and executions overlay on the chart. ``null``
   * = no bound/recent run yet; the card still draws live SPY candles. */
  readonly runId = input<string | null>(null);
  /** The traded symbol. For now the only live instance is SPY; future work
   * surfaces this through the instance status payload. */
  readonly symbol = input<string>('SPY');
  /** Bar resolution. ``'1m'`` reads the aggregated 1-minute buffer; ``'5s'``
   * reads the raw 5-second buffer. The trade-marker overlay is identical
   * across resolutions — the markers land at their absolute timestamps. */
  readonly resolution = input<ChartResolution>('1m');

  protected readonly meta = computed(() => RESOLUTION_META[this.resolution()]);

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

  // Cursor for incremental polling — start_ms of the newest bar we hold.
  private readonly barsSinceMs = signal<number | null>(null);
  private readonly barsCache = signal<IbkrMinuteBar[]>([]);

  protected readonly barsResource = resource<
    IbkrBarsSnapshot,
    { symbol: string; since: number | null; endpoint: string }
  >({
    params: () => ({
      symbol: this.symbol(),
      since: this.barsSinceMs(),
      endpoint: this.meta().endpoint,
    }),
    loader: ({ params }) => this.loadBars(params.endpoint, params.symbol, params.since),
  });

  protected readonly tradesResource = resource<TradeRow[], string | null>({
    params: () => this.runId(),
    loader: ({ params }) => this.loadTrades(params),
  });

  protected readonly executionsResource = resource<ExecutionRow[], string | null>({
    params: () => this.runId(),
    loader: ({ params }) => this.loadExecutions(params),
  });

  protected readonly statusLabel = computed<string>(() => {
    const snap = this.barsResource.value();
    if (!snap) {
      return this.barsResource.isLoading() ? 'Subscribing…' : '';
    }
    if (snap.status === 'streaming') {
      return snap.bars.length === 0 && this.barsCache().length === 0
        ? 'Waiting for first 1-min bar…'
        : 'Streaming';
    }
    if (snap.status === 'errored') {
      return `Error: ${snap.last_error ?? 'unknown'}`;
    }
    if (snap.status === 'subscribing') return 'Subscribing…';
    return '';
  });

  protected readonly statusTone = computed<'ok' | 'warn' | 'bad' | 'idle'>(() => {
    const snap = this.barsResource.value();
    if (snap?.status === 'streaming') return 'ok';
    if (snap?.status === 'errored') return 'bad';
    if (snap?.status === 'subscribing' || this.barsResource.isLoading()) return 'warn';
    return 'idle';
  });

  protected readonly tradeCount = computed<number>(
    () => this.tradesResource.value()?.length ?? 0,
  );

  constructor() {
    afterNextRender(() => this.initChart());

    // Append newly-arrived bars to the cache and update the cursor.
    effect(() => {
      const snap = this.barsResource.value();
      if (!snap) return;
      const incoming = snap.bars;
      if (incoming.length === 0) return;
      const merged = [...this.barsCache(), ...incoming];
      // Defensive de-dup by start_ms; aggregator should never repeat,
      // but a torn poll shouldn't produce duplicate keys for the chart.
      const dedup = Array.from(
        new Map(merged.map((b) => [b.start_ms, b])).values(),
      ).sort((a, b) => a.start_ms - b.start_ms);
      this.barsCache.set(dedup);
      const newest = dedup[dedup.length - 1];
      this.barsSinceMs.set(newest.start_ms);
    });

    effect(() => {
      this.barsCache();
      if (this.chart) this.syncCandles();
    });
    effect(() => {
      this.tradesResource.value();
      this.barsCache();
      if (this.chart) this.syncMarkers();
    });
    effect(() => {
      this.tradesResource.value();
      this.executionsResource.value();
      if (this.chart) this.syncActiveLine();
    });

    this.pollTimer = setInterval(() => {
      this.barsResource.reload();
      this.tradesResource.reload();
      this.executionsResource.reload();
    }, POLL_INTERVAL_MS);

    this.destroyRef.onDestroy(() => {
      if (this.pollTimer !== null) clearInterval(this.pollTimer);
      this.resizeObserver?.disconnect();
      this.chart?.remove();
      this.chart = null;
    });
  }

  private async loadBars(
    endpoint: string,
    symbol: string,
    sinceMs: number | null,
  ): Promise<IbkrBarsSnapshot> {
    const params: Record<string, string | number> = { symbol };
    if (sinceMs !== null) params['since_ms'] = sinceMs;
    return firstValueFrom(this.http.get<IbkrBarsSnapshot>(endpoint, { params }));
  }

  private async loadTrades(runId: string | null): Promise<TradeRow[]> {
    if (!runId) return [];
    return firstValueFrom(
      this.http.get<TradeRow[]>(`/api/live-runs/${encodeURIComponent(runId)}/trades`),
    );
  }

  private async loadExecutions(runId: string | null): Promise<ExecutionRow[]> {
    if (!runId) return [];
    return firstValueFrom(
      this.http.get<ExecutionRow[]>(
        `/api/live-runs/${encodeURIComponent(runId)}/executions`,
      ),
    );
  }

  private initChart(): void {
    const el = this.container().nativeElement;
    const m = this.meta();
    const crosshairTimeFormatter = (time: Time): string => {
      // ``time`` is a UTCTimestamp (seconds since epoch) for our live bars
      // because every series feeds candles built from ``start_ms / 1000``.
      // BusinessDay shape can't appear in this chart, so a plain numeric
      // coercion is safe.
      const d = new Date((time as number) * 1000);
      return m.secondsVisible
        ? d.toLocaleTimeString(undefined, { hour12: false })
        : d.toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          });
    };
    this.chart = createChart(el, {
      width: el.clientWidth,
      height: m.chartHeight,
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
        // ``timeVisible`` keeps the bottom-axis labels on for both
        // resolutions; ``secondsVisible`` is what flips HH:MM ↔ HH:MM:SS
        // so the 5s chart gets per-tick second resolution without forcing
        // the 1m chart's labels to widen.
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

    this.resizeObserver = new ResizeObserver((entries) => {
      if (!this.chart || !entries.length) return;
      const { width } = entries[0].contentRect;
      this.chart.applyOptions({ width });
    });
    this.resizeObserver.observe(el);
  }

  private syncCandles(): void {
    if (!this.candles) return;
    const bars = this.barsCache();
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

  private syncMarkers(): void {
    if (!this.markersPlugin) return;
    const trades = this.tradesResource.value() ?? [];
    const out: SeriesMarker<Time>[] = [];
    trades.forEach((t, i) => {
      out.push({
        time: (t.entry_time_ms / 1000) as UTCTimestamp,
        position: 'belowBar',
        color: '#22c55e',
        shape: 'arrowUp',
        text: `BUY #${i + 1}`,
      });
      out.push({
        time: (t.exit_time_ms / 1000) as UTCTimestamp,
        position: 'aboveBar',
        color: t.pnl_points >= 0 ? '#4ade80' : '#ef4444',
        shape: 'circle',
        text: `CLOSE ${t.pnl_points >= 0 ? '+' : ''}${t.pnl_points.toFixed(2)}`,
      });
    });
    out.sort((a, b) => (a.time as number) - (b.time as number));
    this.markersPlugin.setMarkers(out);
  }

  /** Draw a dashed entry-price line for the active (un-exited) trade.
   *
   * The deployment_validation strategy enters then exits 3 bars later, so
   * an "active" trade is a fill row in executions.parquet for which no
   * exit row exists in trades.parquet yet. We approximate that as: the
   * most-recent execution's fill_price exists but is later than the
   * newest closed trade's exit_time_ms. */
  private syncActiveLine(): void {
    if (!this.candles) return;
    if (this.activeLine) {
      this.candles.removePriceLine(this.activeLine);
      this.activeLine = null;
    }
    const trades = this.tradesResource.value() ?? [];
    const execs = this.executionsResource.value() ?? [];
    if (execs.length === 0) return;

    const lastExit = trades.length > 0 ? trades[trades.length - 1].exit_time_ms : 0;
    const lastExec = execs[execs.length - 1];
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
