import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { PageGuideComponent } from '../../../shared/page-guide/page-guide.component';
import { DataSourceComponent } from '../../../shared/data-source/data-source.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { BrokerService } from '../../../services/broker.service';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { brokerSse, type SseStatus, type SseStream } from '../../../services/broker-sse';
import { QuantLibService } from '../../../services/quantlib.service';
import type {
  IbkrChainSnapshot,
  IbkrOptionQuote,
  OptionRight,
} from '../../../api/broker-models';
import {
  absDiff,
  deltaAbsBand,
  fmtCurrency,
  fmtDateNy,
  fmtNumber,
  fmtPercent,
  fmtTimestampNy,
  type ToleranceBand,
} from '../format';

const DEFAULT_SYMBOL = 'SPY';
const DEFAULT_DEBOUNCE_MS = 500;
const DEFAULT_ATM_BAND = 20; // ± dollars from spot when auto-selecting
const ENGINE_REPRICE_THRESHOLD_CENTS = 1; // re-call engine when bid/ask mid moves >= 1 cent

interface Row {
  strike: number;
  call: IbkrOptionQuote | null;
  put: IbkrOptionQuote | null;
  callEngineDelta: number | null;
  putEngineDelta: number | null;
  callEngineGamma: number | null;
  putEngineGamma: number | null;
  /**
   * |IBKR Δ − Engine Δ| in native units. Replaces the old bps form,
   * which divided by ``|engine Δ|`` and exploded to billions for OTM
   * strikes where the engine delta rounds to ~1e-8 (e.g. SPY 639P at
   * spot 717: IBKR=-0.003, Engine≈-0e-9 → -3,028,671,573 bps in
   * the prior representation). The absolute form is stable across the
   * whole strike range.
   */
  callDeltaAbsDiff: number | null;
  putDeltaAbsDiff: number | null;
  callDeltaBand: ToleranceBand | null;
  putDeltaBand: ToleranceBand | null;
}

interface ExpirationOption {
  ms: number;
  label: string;
}

/**
 * /broker/options-chain — live SPY chain with IBKR vs engine Greeks.
 *
 * Stream lifecycle: pick an expiry + strike band → open one SSE
 * connection → re-render the table per snapshot. ``Pause`` closes the
 * source so backend ``cancelMktData`` fires for every contract. The
 * engine Greek column polls ``QuantLibService.priceOption`` per
 * (strike, right) when the bid/ask mid moves more than a cent.
 */
@Component({
  selector: 'app-broker-options-chain',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    PageHeaderComponent,
    PageGuideComponent,
    DataSourceComponent,
    SectionErrorComponent,
  ],
  styleUrl: './broker-options-chain.component.scss',
  templateUrl: './broker-options-chain.component.html',
})
export class BrokerOptionsChainComponent {
  private readonly broker = inject(BrokerService);
  private readonly health = inject(BrokerHealthService);
  readonly bannerState = this.health.bannerState;
  private readonly quantlib = inject(QuantLibService);
  private readonly injector = inject(Injector);

  readonly symbol = signal(DEFAULT_SYMBOL);
  readonly expirations = signal<ExpirationOption[]>([]);
  readonly selectedExpiry = signal<number | null>(null);
  readonly availableStrikes = signal<number[]>([]);
  readonly selectedStrikes = signal<ReadonlySet<number>>(new Set());
  readonly strikesLoading = signal(false);
  readonly setupError = signal<unknown>(null);
  readonly setupLoading = signal(false);
  readonly paused = signal(false);

  readonly selectedStrikesCount = computed(() => this.selectedStrikes().size);

  /**
   * Active SSE stream. Held in a signal so the template can read its
   * status / latest signals via computed. ``startStream`` swaps it out
   * after explicitly closing the previous one.
   */
  private readonly currentStream = signal<SseStream<IbkrChainSnapshot> | null>(null);
  readonly streamStatus = computed<SseStatus | 'idle'>(() =>
    this.currentStream()?.status() ?? 'idle',
  );
  readonly streamError = signal<unknown>(null);
  readonly latestSnapshot = computed<IbkrChainSnapshot | null>(
    () => this.currentStream()?.latest() ?? null,
  );

  /** Engine reprice cache keyed by `${strike}:${right}` → last bid/ask mid we priced at. */
  private readonly engineMidByKey = new Map<string, number>();
  private readonly engineGreeksByKey = signal<
    Map<string, { delta: number; gamma: number }>
  >(new Map());

  // Monotonic sequence so a slow strike-fetch for an old (symbol, expiry)
  // cannot overwrite the result of a newer one. Increment on every issue.
  private strikesRequestSeq = 0;

  readonly fmtCurrency = fmtCurrency;
  readonly fmtNumber = fmtNumber;
  readonly fmtPercent = fmtPercent;
  readonly fmtTimestampNy = fmtTimestampNy;
  readonly fmtDateNy = fmtDateNy;

  readonly canStream = computed(() => {
    const h = this.health.health();
    return h !== null && h.connected;
  });

  /** Per-strike row, keyed by strike, sorted ascending. */
  readonly rows = computed<Row[]>(() => {
    const snap = this.latestSnapshot();
    const engineGreeks = this.engineGreeksByKey();
    if (snap === null) return [];
    const byStrike = new Map<number, { call: IbkrOptionQuote | null; put: IbkrOptionQuote | null }>();
    for (const q of snap.quotes) {
      const slot = byStrike.get(q.strike) ?? { call: null, put: null };
      if (q.right === 'C') slot.call = q;
      else slot.put = q;
      byStrike.set(q.strike, slot);
    }
    const sorted = [...byStrike.entries()].sort((a, b) => a[0] - b[0]);
    return sorted.map(([strike, { call, put }]) => {
      const callEng = engineGreeks.get(`${strike}:C`) ?? null;
      const putEng = engineGreeks.get(`${strike}:P`) ?? null;
      const callDiff = absDiff(call?.delta ?? null, callEng?.delta ?? null);
      const putDiff = absDiff(put?.delta ?? null, putEng?.delta ?? null);
      return {
        strike,
        call,
        put,
        callEngineDelta: callEng?.delta ?? null,
        putEngineDelta: putEng?.delta ?? null,
        callEngineGamma: callEng?.gamma ?? null,
        putEngineGamma: putEng?.gamma ?? null,
        callDeltaAbsDiff: callDiff,
        putDeltaAbsDiff: putDiff,
        callDeltaBand: deltaAbsBand(callDiff),
        putDeltaBand: deltaAbsBand(putDiff),
      };
    });
  });

  readonly underlyingPrice = computed(() => this.latestSnapshot()?.underlying_price ?? null);
  readonly snapshotAge = computed(() => {
    const snap = this.latestSnapshot();
    if (snap === null) return null;
    return Date.now() - snap.as_of_ms;
  });

  constructor() {
    void this.loadExpirations();

    // When the expiry changes, refetch the qualifiable strikes for the
    // new (symbol, expiry) and reset any previous selection. Bumping the
    // sequence in both branches invalidates any in-flight loadStrikes
    // that is about to resolve from a now-stale request.
    effect(() => {
      const expiry = this.selectedExpiry();
      const sym = this.symbol();
      if (expiry === null) {
        this.strikesRequestSeq++;
        this.availableStrikes.set([]);
        this.selectedStrikes.set(new Set());
        return;
      }
      void this.loadStrikes(sym, expiry);
    });

    // Recompute engine Greeks any time a snapshot arrives. Throttled
    // implicitly by the per-key mid threshold.
    effect(() => {
      const snap = this.latestSnapshot();
      const expiry = this.selectedExpiry();
      if (snap === null || expiry === null) return;
      this.maybeRepriceEngine(snap, expiry);
    });

    // Mirror SSE error onto our local signal so it survives stream
    // teardown and is presented uniformly with setup errors.
    effect(() => {
      const err = this.currentStream()?.lastError() ?? null;
      this.streamError.set(err);
    });
  }

  async loadExpirations(): Promise<void> {
    if (!this.canStream()) return;
    this.setupLoading.set(true);
    this.setupError.set(null);
    try {
      const result = await this.broker.expirations(this.symbol());
      const opts: ExpirationOption[] = result.expirations_ms.map((ms) => ({
        ms,
        label: fmtDateNy(ms),
      }));
      this.expirations.set(opts);
      // Default to nearest expiry.
      const now = Date.now();
      const next = opts.find((o) => o.ms >= now) ?? opts[0];
      if (next) this.selectedExpiry.set(next.ms);
    } catch (err) {
      this.setupError.set(err);
    } finally {
      this.setupLoading.set(false);
    }
  }

  startStream(): void {
    const expiry = this.selectedExpiry();
    const strikes = [...this.selectedStrikes()].sort((a, b) => a - b);
    if (expiry === null) {
      this.streamError.set(new Error('Pick an expiry first.'));
      return;
    }
    if (strikes.length === 0) {
      this.streamError.set(new Error('Pick at least one strike.'));
      return;
    }

    // Tear down any previous stream first.
    this.stopStream();

    this.streamError.set(null);
    this.paused.set(false);
    this.engineMidByKey.clear();
    this.engineGreeksByKey.set(new Map());

    const params = new URLSearchParams();
    params.set('expiry_ms', String(expiry));
    for (const k of strikes) params.append('strikes', String(k));
    params.set('debounce_ms', String(DEFAULT_DEBOUNCE_MS));
    const url =
      `/api/broker/option-chain/${encodeURIComponent(this.symbol())}` +
      `?${params.toString()}`;

    // ``brokerSse`` calls ``inject(DestroyRef)`` to wire up cleanup —
    // run it inside this component's injector so the EventSource gets
    // closed when the route navigates away.
    const stream = runInInjectionContext(this.injector, () =>
      brokerSse<IbkrChainSnapshot>(url, 'chain', { maxBuffer: 1 }),
    );
    this.currentStream.set(stream);
  }

  stopStream(): void {
    const stream = this.currentStream();
    if (stream) {
      stream.close();
    }
    this.currentStream.set(null);
    this.paused.set(true);
  }

  togglePause(): void {
    if (this.paused()) {
      this.startStream();
    } else {
      this.stopStream();
    }
  }

  async loadStrikes(symbol: string, expiryMs: number): Promise<void> {
    const seq = ++this.strikesRequestSeq;
    this.strikesLoading.set(true);
    this.setupError.set(null);
    try {
      const resp = await this.broker.strikes(symbol, expiryMs);
      // Race guard: if a newer (symbol, expiry) has been requested while
      // we were waiting, drop this response on the floor — applying it
      // would rewrite the user's freshly-loaded strike chips.
      if (seq !== this.strikesRequestSeq) return;
      this.availableStrikes.set(resp.strikes);
      this.selectedStrikes.set(new Set());
    } catch (err) {
      if (seq !== this.strikesRequestSeq) return;
      this.availableStrikes.set([]);
      this.selectedStrikes.set(new Set());
      this.setupError.set(err);
    } finally {
      if (seq === this.strikesRequestSeq) {
        this.strikesLoading.set(false);
      }
    }
  }

  toggleStrike(strike: number): void {
    this.selectedStrikes.update((current) => {
      const next = new Set(current);
      if (next.has(strike)) next.delete(strike);
      else next.add(strike);
      return next;
    });
  }

  isStrikeSelected(strike: number): boolean {
    return this.selectedStrikes().has(strike);
  }

  selectAtmBand(): void {
    const spot = this.underlyingPrice();
    const available = this.availableStrikes();
    if (spot === null || available.length === 0) return;
    const lo = spot - DEFAULT_ATM_BAND;
    const hi = spot + DEFAULT_ATM_BAND;
    const next = new Set<number>(available.filter((k) => k >= lo && k <= hi));
    this.selectedStrikes.set(next);
  }

  clearStrikeSelection(): void {
    this.selectedStrikes.set(new Set());
  }

  trackRow = (_: number, row: Row): number => row.strike;

  private maybeRepriceEngine(snap: IbkrChainSnapshot, expiryMs: number): void {
    const spot = snap.underlying_price;
    if (spot === null) return;
    const expirationDate = isoDateFromMs(expiryMs);
    if (expirationDate === null) return;

    const promises: Promise<void>[] = [];
    for (const q of snap.quotes) {
      const mid = midOrNull(q);
      if (mid === null) continue;
      const key = `${q.strike}:${q.right}`;
      const lastMid = this.engineMidByKey.get(key);
      if (lastMid !== undefined && Math.abs(lastMid - mid) < ENGINE_REPRICE_THRESHOLD_CENTS / 100) {
        continue;
      }
      this.engineMidByKey.set(key, mid);
      const iv = q.iv ?? 0.2; // when IBKR has no IV, keep a benign placeholder
      promises.push(
        this.repriceEngine({
          strike: q.strike,
          right: q.right,
          spot,
          iv,
          expirationDate,
        }),
      );
    }
    void Promise.all(promises);
  }

  private async repriceEngine(args: {
    strike: number;
    right: OptionRight;
    spot: number;
    iv: number;
    expirationDate: string;
  }): Promise<void> {
    try {
      const result = await this.quantlib.priceOption({
        spot: args.spot,
        strike: args.strike,
        volatility: args.iv,
        expirationDate: args.expirationDate,
        optionType: args.right === 'C' ? 'call' : 'put',
      });
      if (!result.success) return;
      const key = `${args.strike}:${args.right}`;
      this.engineGreeksByKey.update((m) => {
        const next = new Map(m);
        next.set(key, { delta: result.delta, gamma: result.gamma });
        return next;
      });
    } catch {
      // Engine reprice errors are non-fatal — the column simply stays
      // blank for that row and the diff column reads ``—``.
    }
  }
}

function midOrNull(q: IbkrOptionQuote): number | null {
  if (q.bid === null || q.ask === null) return null;
  const mid = (q.bid + q.ask) / 2;
  // Reject non-positive mids: (a) the engine reprice trigger compares
  // mids with a 1¢ threshold, so a bogus negative mid would cause
  // false-positive reprices; (b) once the Python side strips IBKR's
  // ``-1`` "no quote" sentinel from bid/ask the upstream null-check
  // already covers most cases, but a defensive bound here keeps the
  // engine off bad inputs even if a future surface forgets to.
  return mid > 0 ? mid : null;
}

function isoDateFromMs(ms: number): string | null {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return null;
  // Convert in UTC. The backend stores option expiries as midnight-UTC
  // date markers (``yyyymmdd_to_expiry_ms`` parses ``YYYYMMDD`` with
  // ``tzinfo=UTC``), so the round-trip from ms back to ``YYYY-MM-DD``
  // must also use UTC. Converting in ET would shift expiries one day
  // earlier whenever ``expiry_ms`` lands at 00:00 UTC and skew
  // QuantLib's time-to-expiry near the close.
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
  return fmt.format(d); // en-CA emits YYYY-MM-DD
}

