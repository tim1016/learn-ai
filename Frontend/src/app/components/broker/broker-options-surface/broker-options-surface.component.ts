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
import { RouterLink } from '@angular/router';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { PageGuideComponent } from '../../../shared/page-guide/page-guide.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { BrokerService } from '../../../services/broker.service';
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
    RouterLink,
  ],
  styleUrl: './broker-options-surface.component.scss',
  templateUrl: './broker-options-surface.component.html',
})
export class BrokerOptionsSurfaceComponent {
  private readonly broker = inject(BrokerService);
  private readonly health = inject(BrokerHealthService);
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);
  readonly bannerState = this.health.bannerState;

  readonly symbol = signal(DEFAULT_SYMBOL);
  readonly daysOut = signal(DEFAULT_DAYS_OUT);
  readonly strikesBand = signal(DEFAULT_STRIKES_BAND);
  readonly debounceMs = signal(DEFAULT_DEBOUNCE_MS);
  readonly zMetric = signal<ZMetric>('last');
  readonly monthliesOnly = signal(true);

  /** All expiries reported by /api/broker/expirations. */
  readonly allExpirations = signal<number[]>([]);
  /** Strikes per expiry, populated lazily by setupSurface(). */
  private strikesByExpiry = new Map<number, number[]>();
  /** Best-effort spot price used to pick the strike band. Null until first snapshot. */
  readonly spotPrice = signal<number | null>(null);

  readonly setupLoading = signal(false);
  readonly setupError = signal<unknown>(null);
  readonly paused = signal(false);
  readonly readyExpiries = signal<number[]>([]);
  readonly readyStrikes = signal<number[]>([]);

  readonly zMetrics = Z_METRICS;

  private readonly currentStream = signal<SseStream<IbkrSurfaceSnapshot> | null>(null);
  readonly streamStatus = computed<SseStatus | 'idle'>(
    () => this.currentStream()?.status() ?? 'idle',
  );
  readonly streamError = signal<unknown>(null);
  readonly latestSnapshot = computed<IbkrSurfaceSnapshot | null>(
    () => this.currentStream()?.latest() ?? null,
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

  readonly underlyingPrice = computed(
    () => this.latestSnapshot()?.underlying_price ?? this.spotPrice(),
  );
  readonly snapshotAge = computed(() => {
    const snap = this.latestSnapshot();
    if (snap === null) return null;
    return Date.now() - snap.as_of_ms;
  });

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
      this.renderCharts();
    });

    // Auto-load expirations on first paint when the broker is live.
    effect(() => {
      if (this.canStream() && this.allExpirations().length === 0 && !this.setupLoading()) {
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
    this.setupLoading.set(true);
    this.setupError.set(null);
    try {
      const result = await this.broker.expirations(this.symbol());
      this.allExpirations.set(result.expirations_ms);
    } catch (err) {
      this.setupError.set(err);
    } finally {
      this.setupLoading.set(false);
    }
  }

  /**
   * Pick expiries within the user-chosen window. If monthliesOnly is on
   * we keep only the third-Friday-style monthlies (heuristic: largest
   * gap from the prior expiry > 6 days), which gives the typical ~2
   * monthlies inside a 60-day window and keeps the line count well
   * under the cap.
   */
  pickExpiries(): number[] {
    const now = Date.now();
    const cutoff = now + this.daysOut() * 24 * 60 * 60 * 1000;
    const inWindow = this.allExpirations().filter((ms) => ms >= now && ms <= cutoff);
    if (!this.monthliesOnly()) return inWindow;
    // Heuristic: the gap between a monthly and the prior weekly is
    // typically a week or more. Sort and accept entries where the prior
    // entry was > 6 days earlier (or it's the first).
    const sorted = [...inWindow].sort((a, b) => a - b);
    const monthlies: number[] = [];
    let prev = 0;
    for (const ms of sorted) {
      if (prev === 0 || ms - prev > 6 * 24 * 60 * 60 * 1000) {
        monthlies.push(ms);
      }
      prev = ms;
    }
    return monthlies;
  }

  /**
   * For each candidate expiry, pull the qualifiable strikes and pick
   * the intersection within ATM ± strikesBand. The intersection is what
   * we hand the surface stream; everything beyond would partially
   * qualify and the backend would refuse.
   */
  async setupSurface(): Promise<void> {
    if (!this.canStream()) return;
    this.setupLoading.set(true);
    this.setupError.set(null);
    try {
      const sym = this.symbol();
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
      for (const list of lists) this.strikesByExpiry.set(list.expiry_ms, list.strikes);

      // Spot is needed to centre the strike band. Use the latest snapshot
      // if we have one (from a previous load) or fall back to the median
      // strike of the nearest expiry as a sanity centre.
      let spot = this.underlyingPrice();
      if (spot === null) {
        const first = this.strikesByExpiry.get(expiries[0]) ?? [];
        if (first.length > 0) spot = first[Math.floor(first.length / 2)];
      }
      this.spotPrice.set(spot);
      if (spot === null) {
        throw new Error('Could not resolve a spot price to centre the strike band.');
      }

      const band = this.strikesBand();
      const perExpiry: number[][] = expiries.map((ms) => {
        const all = this.strikesByExpiry.get(ms) ?? [];
        return pickStrikesAroundAtm(all, spot as number, band);
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
      this.setupError.set(err);
    } finally {
      this.setupLoading.set(false);
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

    const params = new URLSearchParams();
    for (const e of expiries) params.append('expiry_ms', String(e));
    for (const k of strikes) params.append('strikes', String(k));
    params.set('debounce_ms', String(this.debounceMs()));
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

// Re-export for tests.
export const __TEST__ = { pickStrikesAroundAtm, intersectAll };
