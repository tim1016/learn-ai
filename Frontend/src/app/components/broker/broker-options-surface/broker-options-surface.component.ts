import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { PageGuideComponent } from '../../../shared/page-guide/page-guide.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { MarketDataFeedService } from '../../../services/market-data-feed.service';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { brokerSse, type SseStatus, type SseStream } from '../../../services/broker-sse';
import type {
  IbkrSurfaceSnapshot,
} from '../../../api/broker-models';
import { fmtBrokerExpiryDate, fmtCurrency, fmtTimestampNy } from '../format';

const DEFAULT_SYMBOL = 'SPY';
const DEFAULT_DEBOUNCE_MS = 500;
const DEFAULT_DAYS_OUT = 60;
const DEFAULT_STRIKES_BAND = 5;
const LINE_CAP = 100;

/**
 * Selectable Z-axis metric. Sourced from the per-quote payload
 * IBKR sends; mid is computed locally from bid/ask, the rest come
 * straight off `IbkrOptionQuote`.
 */
export type ZMetric =
  | 'last'
  | 'mid'
  | 'bid'
  | 'ask'
  | 'iv'
  | 'delta'
  | 'gamma'
  | 'theta'
  | 'vega';

interface ZMetricOption {
  value: ZMetric;
  label: string;
}

const Z_METRICS: readonly ZMetricOption[] = [
  { value: 'last', label: 'Last' },
  { value: 'mid', label: 'Mid' },
  { value: 'bid', label: 'Bid' },
  { value: 'ask', label: 'Ask' },
  { value: 'iv', label: 'IV' },
  { value: 'delta', label: 'Δ Delta' },
  { value: 'gamma', label: 'Γ Gamma' },
  { value: 'theta', label: 'Θ Theta' },
  { value: 'vega', label: 'ν Vega' },
] as const;

/**
 * /broker/options-surface — IBKR live multi-expiry option surface as a
 * pair of 3D bar charts (call | put).
 *
 * Y axis = strikes (default ATM ± 5), X axis = expiries (default 2
 * months out), Z axis = selectable metric (default last). The strike
 * band and expiry window are intersected against IBKR's qualifiable
 * grid so the underlying SSE subscription never asks for a phantom
 * contract.
 *
 * Backend cap: the surface stream refuses to subscribe more than
 * ``LINE_CAP`` market-data lines (1 underlying + N×M×2 options). This
 * component pre-projects the count, surfaces it as a badge, and
 * disables the Load button when over.
 */
@Component({
  selector: 'app-broker-options-surface',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    PageHeaderComponent,
    PageGuideComponent,
    SectionErrorComponent,
  ],
  styleUrl: './broker-options-surface.component.scss',
  templateUrl: './broker-options-surface.component.html',
})
export class BrokerOptionsSurfaceComponent {
  private readonly broker = inject(MarketDataFeedService);
  private readonly health = inject(BrokerHealthService);
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);
  readonly bannerState = this.health.bannerState;
  private expirationsRequested = false;
  private setupRequestSeq = 0;

  readonly symbol = signal(DEFAULT_SYMBOL);
  readonly daysOut = signal(DEFAULT_DAYS_OUT);
  readonly strikesBand = signal(DEFAULT_STRIKES_BAND);
  readonly zMetric = signal<ZMetric>('last');
  readonly monthliesOnly = signal(true);

  /** All expiries reported by /api/broker/expirations. */
  readonly allExpirations = signal<number[]>([]);
  /** Strikes per expiry, populated lazily by setupSurface(). */
  private strikesByExpiry = new Map<number, number[]>();
  readonly setupLoading = signal(false);
  readonly setupError = signal<unknown>(null);
  readonly chartError = signal<unknown>(null);
  readonly paused = signal(false);
  readonly readyExpiries = signal<number[]>([]);
  readonly readyStrikes = signal<number[]>([]);

  readonly zMetrics = Z_METRICS;

  private readonly currentStream = signal<SseStream<IbkrSurfaceSnapshot> | null>(null);
  readonly streamStatus = computed<SseStatus | 'idle'>(
    () => this.currentStream()?.status() ?? 'idle',
  );
  readonly streamError = signal<unknown>(null);
  private readonly lastSnapshot = signal<IbkrSurfaceSnapshot | null>(null);
  readonly latestSnapshot = computed<IbkrSurfaceSnapshot | null>(
    () => this.currentStream()?.latest() ?? this.lastSnapshot(),
  );

  readonly canStream = computed(() => {
    const h = this.health.health();
    return h !== null && h.connected;
  });

  readonly currentStreamActive = computed(() => this.currentStream() !== null);

  /**
   * Pre-projected line count for the current (expiries × strikes × 2 + 1).
   * Used to render the "lines: X / 100" badge and to disable Load when
   * the request would be rejected by the backend cap.
   */
  readonly projectedLines = computed(() => {
    const exps = this.readyExpiries().length;
    const ks = this.readyStrikes().length;
    if (exps === 0 || ks === 0) return 0;
    return 1 + exps * ks * 2;
  });

  readonly overCap = computed(() => this.projectedLines() > LINE_CAP);

  readonly underlyingPrice = computed(() => this.latestSnapshot()?.underlying_price ?? null);

  /** Currently-rendered grid: sorted unique strikes and expiries. */
  readonly axesY = computed<number[]>(() => {
    const snap = this.latestSnapshot();
    if (snap === null) return [];
    const strikes = new Set<number>();
    for (const g of snap.expiries) for (const q of g.quotes) strikes.add(q.strike);
    return [...strikes].sort((a, b) => a - b);
  });

  readonly axesX = computed<number[]>(() => {
    const snap = this.latestSnapshot();
    if (snap === null) return [];
    return snap.expiries.map((e) => e.expiry_ms);
  });

  /** Tile values laid out as a single flat [xIdx, yIdx, value] series per side. */
  readonly callSeries = computed<[number, number, number][]>(() =>
    this.buildSeries('C'),
  );
  readonly putSeries = computed<[number, number, number][]>(() =>
    this.buildSeries('P'),
  );

  // ── Chart canvases ────────────────────────────────────────────────

  readonly callHostRef = viewChild<ElementRef<HTMLDivElement>>('callHost');
  readonly putHostRef = viewChild<ElementRef<HTMLDivElement>>('putHost');

  private callChart: unknown = null;
  private putChart: unknown = null;

  readonly fmtCurrency = fmtCurrency;
  readonly fmtTimestampNy = fmtTimestampNy;

  readonly zMetricLabel = computed(
    () => Z_METRICS.find((m) => m.value === this.zMetric())?.label ?? this.zMetric(),
  );

  constructor() {
    this.health.start();
    effect(() => {
      const snapshot = this.currentStream()?.latest() ?? null;
      if (snapshot !== null) this.lastSnapshot.set(snapshot);
    });
    // Mirror stream error onto our local signal.
    effect(() => {
      const err = this.currentStream()?.lastError() ?? null;
      this.streamError.set(err);
    });

    // Initialise / re-render the 3D canvases whenever the data or the
    // selected metric changes. Lazy-load echarts on first paint so the
    // component bundle stays small.
    effect(() => {
      // Track signals so the effect re-runs when they change.
      void this.callSeries();
      void this.putSeries();
      void this.axesX();
      void this.axesY();
      void this.zMetric();
      void this.renderCharts().catch((error: unknown) => this.chartError.set(error));
    });

    // Auto-load expirations on first paint when the broker is live.
    effect(() => {
      if (this.canStream() && !this.expirationsRequested) {
        this.expirationsRequested = true;
        void this.loadExpirations();
      }
    });

    // Best-effort window resize handling. ECharts only resizes on demand.
    window.addEventListener('resize', this.onResize, { passive: true });
    this.destroyRef.onDestroy(() => {
      window.removeEventListener('resize', this.onResize);
      this.disposeCharts();
    });
  }

  // ── Setup / streaming ─────────────────────────────────────────────

  async loadExpirations(): Promise<void> {
    if (!this.canStream()) return;
    const seq = ++this.setupRequestSeq;
    const symbol = this.symbol();
    this.setupLoading.set(true);
    this.setupError.set(null);
    try {
      const result = await this.broker.expirations(symbol);
      if (seq !== this.setupRequestSeq) return;
      this.allExpirations.set(result.expirations_ms);
    } catch (err) {
      if (seq === this.setupRequestSeq) this.setupError.set(err);
    } finally {
      if (seq === this.setupRequestSeq) this.setupLoading.set(false);
    }
  }

  setSymbol(symbol: string): void {
    if (symbol === this.symbol()) return;
    this.invalidateSetup();
    this.symbol.set(symbol);
    this.allExpirations.set([]);
    this.strikesByExpiry.clear();
  }

  setDaysOut(days: number): void {
    if (days === this.daysOut()) return;
    this.invalidateSetup();
    this.daysOut.set(days);
  }

  setStrikesBand(band: number): void {
    if (band === this.strikesBand()) return;
    this.invalidateSetup();
    this.strikesBand.set(band);
  }

  setMonthliesOnly(monthliesOnly: boolean): void {
    if (monthliesOnly === this.monthliesOnly()) return;
    this.invalidateSetup();
    this.monthliesOnly.set(monthliesOnly);
  }

  private invalidateSetup(): void {
    this.setupRequestSeq++;
    this.stopStream();
    this.paused.set(false);
    this.lastSnapshot.set(null);
    this.readyExpiries.set([]);
    this.readyStrikes.set([]);
    this.setupError.set(null);
    this.setupLoading.set(false);
  }

  /**
   * Pick expiries within the user-chosen window. If monthliesOnly is on
   * we keep each month's listed expiry on or immediately before its
   * third Friday (the Thursday listing covers a Friday market holiday).
   */
  pickExpiries(): number[] {
    const now = Date.now();
    const cutoff = now + this.daysOut() * 24 * 60 * 60 * 1000;
    const inWindow = this.allExpirations().filter((ms) => ms >= now && ms <= cutoff);
    return this.monthliesOnly() ? pickMonthlyExpiries(inWindow) : inWindow;
  }

  /**
   * For each candidate expiry, pull the qualifiable strikes and pick
   * the intersection within ATM ± strikesBand. The intersection is what
   * we hand the surface stream; everything beyond would partially
   * qualify and the backend would refuse.
   */
  async setupSurface(): Promise<void> {
    if (!this.canStream()) return;
    this.stopStream();
    this.paused.set(false);
    this.lastSnapshot.set(null);
    const seq = ++this.setupRequestSeq;
    const sym = this.symbol();
    this.setupLoading.set(true);
    this.setupError.set(null);
    this.readyExpiries.set([]);
    this.readyStrikes.set([]);
    try {
      if (this.allExpirations().length === 0) {
        const result = await this.broker.expirations(sym);
        if (seq !== this.setupRequestSeq) return;
        this.allExpirations.set(result.expirations_ms);
      }
      const expiries = this.pickExpiries();
      if (expiries.length === 0) {
        throw new Error(
          `No expirations within ${this.daysOut()} days. Adjust the days-out filter or disable monthlies-only.`,
        );
      }

      this.strikesByExpiry = new Map();
      const lists = await Promise.all(
        expiries.map((ms) => this.broker.strikes(sym, ms)),
      );
      if (seq !== this.setupRequestSeq) return;
      for (const list of lists) this.strikesByExpiry.set(list.expiry_ms, list.strikes);

      // A strike-grid midpoint can centre selection before the first tick,
      // but it is not a market spot price and must never be labelled as one.
      let centre = this.underlyingPrice();
      if (centre === null) {
        const first = this.strikesByExpiry.get(expiries[0]) ?? [];
        if (first.length > 0) centre = first[Math.floor(first.length / 2)];
      }
      if (centre === null) {
        throw new Error('Could not resolve a strike-grid centre for the band.');
      }
      const selectedCentre = centre;

      const band = this.strikesBand();
      const perExpiry: number[][] = expiries.map((ms) => {
        const all = this.strikesByExpiry.get(ms) ?? [];
        return pickStrikesAroundAtm(all, selectedCentre, band);
      });

      const intersected = intersectAll(perExpiry).sort((a, b) => a - b);
      if (intersected.length === 0) {
        throw new Error(
          'No strikes within the band qualify on every chosen expiry. Widen the band or pick a different window.',
        );
      }

      this.readyExpiries.set(expiries.sort((a, b) => a - b));
      this.readyStrikes.set(intersected);
    } catch (err) {
      if (seq === this.setupRequestSeq) this.setupError.set(err);
    } finally {
      if (seq === this.setupRequestSeq) this.setupLoading.set(false);
    }
  }

  startStream(): void {
    if (!this.canStream()) return;
    if (this.overCap()) {
      this.streamError.set(
        new Error(
          `Projected ${this.projectedLines()} streaming lines exceeds the ${LINE_CAP}-line cap.`,
        ),
      );
      return;
    }
    const expiries = this.readyExpiries();
    const strikes = this.readyStrikes();
    if (expiries.length === 0 || strikes.length === 0) {
      this.streamError.set(new Error('Run Setup first.'));
      return;
    }
    this.stopStream();
    this.streamError.set(null);
    this.paused.set(false);
    this.lastSnapshot.set(null);

    const params = new URLSearchParams();
    for (const e of expiries) params.append('expiry_ms', String(e));
    for (const k of strikes) params.append('strikes', String(k));
    params.set('debounce_ms', String(DEFAULT_DEBOUNCE_MS));
    params.set('max_lines', String(LINE_CAP));
    const url =
      `/api/broker/option-surface/${encodeURIComponent(this.symbol())}` +
      `?${params.toString()}`;

    const stream = runInInjectionContext(this.injector, () =>
      brokerSse<IbkrSurfaceSnapshot>(url, 'surface', { maxBuffer: 1 }),
    );
    this.currentStream.set(stream);
  }

  stopStream(): void {
    const stream = this.currentStream();
    if (stream) stream.close();
    this.currentStream.set(null);
    this.paused.set(true);
  }

  togglePause(): void {
    if (this.paused()) this.startStream();
    else this.stopStream();
  }

  // ── Series projection ─────────────────────────────────────────────

  private buildSeries(side: 'C' | 'P'): [number, number, number][] {
    const snap = this.latestSnapshot();
    if (snap === null) return [];
    const xs = this.axesX();
    const ys = this.axesY();
    const xIdx = new Map<number, number>();
    xs.forEach((ms, i) => xIdx.set(ms, i));
    const yIdx = new Map<number, number>();
    ys.forEach((k, i) => yIdx.set(k, i));
    const out: [number, number, number][] = [];
    const metric = this.zMetric();
    for (const group of snap.expiries) {
      const xi = xIdx.get(group.expiry_ms);
      if (xi === undefined) continue;
      for (const q of group.quotes) {
        if (q.right !== side) continue;
        const yi = yIdx.get(q.strike);
        if (yi === undefined) continue;
        const z = pickMetric(q, metric);
        if (z === null) continue;
        out.push([xi, yi, z]);
      }
    }
    return out;
  }

  // ── Chart lifecycle ───────────────────────────────────────────────

  private readonly onResize = (): void => {
    this.resizeChart(this.callChart);
    this.resizeChart(this.putChart);
  };

  private resizeChart(chart: unknown): void {
    if (chart && typeof (chart as { resize?: () => void }).resize === 'function') {
      (chart as { resize: () => void }).resize();
    }
  }

  private disposeCharts(): void {
    for (const chart of [this.callChart, this.putChart]) {
      if (chart && typeof (chart as { dispose?: () => void }).dispose === 'function') {
        (chart as { dispose: () => void }).dispose();
      }
    }
    this.callChart = null;
    this.putChart = null;
  }

  private async renderCharts(): Promise<void> {
    const callHost = this.callHostRef()?.nativeElement ?? null;
    const putHost = this.putHostRef()?.nativeElement ?? null;
    if (callHost === null || putHost === null) return;

    const xs = this.axesX();
    const ys = this.axesY();
    if (xs.length === 0 || ys.length === 0) {
      this.disposeCharts();
      return;
    }

    // Lazy-load echarts so this page's bundle doesn't bloat the rest.
    const echarts = await import('echarts');
    await import('echarts-gl');
    if (xs !== this.axesX() || ys !== this.axesY()) return;
    this.chartError.set(null);

    const xCategories = xs.map((ms) => fmtBrokerExpiryDate(ms));
    const yCategories = ys.map((k) => k.toFixed(2));

    if (!this.callChart) this.callChart = echarts.init(callHost);
    if (!this.putChart) this.putChart = echarts.init(putHost);

    this.applyOption(this.callChart, 'Calls', xCategories, yCategories, this.callSeries());
    this.applyOption(this.putChart, 'Puts', xCategories, yCategories, this.putSeries());
  }

  /**
   * Build the bar3d-punch-card style option object used by ECharts GL.
   * Distilled from the upstream example referenced in the goal:
   * https://echarts.apache.org/examples/en/editor.html?c=bar3d-punch-card
   */
  private applyOption(
    chart: unknown,
    title: string,
    xCategories: string[],
    yCategories: string[],
    series: [number, number, number][],
  ): void {
    if (!chart) return;
    const zLabel = this.zMetricLabel();
    const option = {
      title: { text: title, left: 'center', textStyle: { fontSize: 14 } },
      tooltip: {
        formatter: (params: { value: [number, number, number] }) => {
          const [xi, yi, z] = params.value;
          return (
            `Expiry: ${xCategories[xi] ?? '?'}<br/>` +
            `Strike: ${yCategories[yi] ?? '?'}<br/>` +
            `${zLabel}: ${formatZ(z, this.zMetric())}`
          );
        },
      },
      xAxis3D: { type: 'category', name: 'Expiry', data: xCategories },
      yAxis3D: { type: 'category', name: 'Strike', data: yCategories },
      zAxis3D: { type: 'value', name: zLabel },
      grid3D: {
        boxWidth: 200,
        boxDepth: 80,
        viewControl: { autoRotate: false },
        light: { main: { intensity: 1.2, shadow: true }, ambient: { intensity: 0.3 } },
      },
      series: [
        {
          type: 'bar3D',
          data: series,
          shading: 'lambert',
          itemStyle: { opacity: 0.85 },
          label: { show: false },
          emphasis: { label: { show: false } },
        },
      ],
    };
    (chart as { setOption: (o: unknown) => void }).setOption(option);
  }
}

// ── Helpers (pure, exported for unit tests) ───────────────────────────

const EXPIRY_DATE = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', year: 'numeric', month: 'numeric', day: 'numeric',
});

export function pickMonthlyExpiries(expiriesMs: number[]): number[] {
  const byMonth = new Map<string, { ms: number; day: number }>();
  for (const ms of expiriesMs) {
    const parts = EXPIRY_DATE.formatToParts(ms);
    const numberPart = (type: string): number => Number(parts.find((part) => part.type === type)?.value);
    const year = numberPart('year');
    const month = numberPart('month');
    const day = numberPart('day');
    const firstWeekday = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
    const thirdFriday = 1 + (5 - firstWeekday + 7) % 7 + 14;
    if (day > thirdFriday || day < thirdFriday - 1) continue;
    const key = `${year}-${month}`;
    const prior = byMonth.get(key);
    if (prior === undefined || day > prior.day) byMonth.set(key, { ms, day });
  }
  return [...byMonth.values()].map(({ ms }) => ms).sort((a, b) => a - b);
}

export function pickStrikesAroundAtm(
  strikes: number[],
  atm: number,
  band: number,
): number[] {
  if (strikes.length === 0) return [];
  const sorted = [...strikes].sort((a, b) => a - b);
  // Find the index of the strike closest to ATM.
  let atmIdx = 0;
  let bestDiff = Math.abs(sorted[0] - atm);
  for (let i = 1; i < sorted.length; i++) {
    const diff = Math.abs(sorted[i] - atm);
    if (diff < bestDiff) {
      bestDiff = diff;
      atmIdx = i;
    }
  }
  const lo = Math.max(0, atmIdx - band);
  const hi = Math.min(sorted.length, atmIdx + band + 1);
  return sorted.slice(lo, hi);
}

export function intersectAll(lists: number[][]): number[] {
  if (lists.length === 0) return [];
  let acc = new Set<number>(lists[0]);
  for (let i = 1; i < lists.length; i++) {
    const next = new Set<number>();
    for (const v of lists[i]) if (acc.has(v)) next.add(v);
    acc = next;
  }
  return [...acc];
}

function pickMetric(
  q: {
    last: number | null;
    bid: number | null;
    ask: number | null;
    iv: number | null;
    delta: number | null;
    gamma: number | null;
    theta: number | null;
    vega: number | null;
  },
  metric: ZMetric,
): number | null {
  switch (metric) {
    case 'last':
      return q.last;
    case 'bid':
      return q.bid;
    case 'ask':
      return q.ask;
    case 'mid':
      return q.bid !== null && q.ask !== null ? (q.bid + q.ask) / 2 : null;
    case 'iv':
      return q.iv;
    case 'delta':
      return q.delta;
    case 'gamma':
      return q.gamma;
    case 'theta':
      return q.theta;
    case 'vega':
      return q.vega;
  }
}

function formatZ(value: number, metric: ZMetric): string {
  if (metric === 'iv') return `${(value * 100).toFixed(2)}%`;
  if (metric === 'gamma') return value.toFixed(6);
  return value.toFixed(4);
}
