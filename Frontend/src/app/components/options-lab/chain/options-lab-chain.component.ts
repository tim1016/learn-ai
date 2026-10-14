import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DecimalPipe } from '@angular/common';
import { interval, firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../../services/market-data.service';
import {
  SnapshotContractResult,
  SnapshotUnderlyingResult,
  StockTickerSnapshot,
} from '../../../graphql/types';
import { ExpirationRibbonComponent } from '../../options-chain-v2/expiration-ribbon/expiration-ribbon.component';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import { etIsoDate } from '../../../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../../../shared/option-contract/option-expiry-date';
import { formatTimestampDisplay } from '../../../shared/timestamp/timestamp-display';
import { errorMessage } from '../../../shared/errors/error-message';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';

interface ChainRow {
  strike: number;
  strikeFormatted: string;
  isAtm: boolean;
  itmCall: boolean;
  itmPut: boolean;
  centerIv: string;

  callLast: string;
  callBid: string;
  callAsk: string;
  callSpread: string;
  callDelta: string;
  callGamma: string;
  callTheta: string;
  callVega: string;
  callVolume: string;
  callVolumeBarWidth: number;
  callOI: string;

  putLast: string;
  putBid: string;
  putAsk: string;
  putSpread: string;
  putDelta: string;
  putGamma: string;
  putTheta: string;
  putVega: string;
  putVolume: string;
  putVolumeBarWidth: number;
  putOI: string;
}

type ChainDensity = 'quick' | 'greeks' | 'flow';
const DENSITY_STORAGE_KEY = 'optionsLab.chain.density';
const POLL_INTERVAL_MS = 5_000;
const EM_DASH = '—';

@Component({
  selector: 'app-options-lab-chain',
  standalone: true,
  imports: [ExpirationRibbonComponent, DecimalPipe, AssetIdentityComponent,
    SymbolPickerComponent, TimestampDisplayComponent,],
  templateUrl: './options-lab-chain.component.html',
  styleUrls: ['./options-lab-chain.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsLabChainComponent implements OnInit {
  private market = inject(MarketDataService);
  private destroyRef = inject(DestroyRef);

  ticker = signal('SPY');

  /** A pick is one deliberate act — load its expirations immediately. */
  onTickerPicked(ticker: string): void {
    this.ticker.set(ticker);
    void this.fetchExpirations();
  }

  expirationsLoading = signal(false);
  chainLoading = signal(false);
  initialLoad = signal(true);
  error = signal<string | null>(null);
  /** When the chain on screen last arrived (`int64 ms UTC`); null before the first one. */
  chainAsOfMs = signal<number | null>(null);
  /** Why the latest background refresh failed, so quotes older than it read as stale. */
  refreshError = signal<string | null>(null);
  /** Counts expiration fetches; only the latest may write the page. */
  private expirationsRequest = 0;
  /** Counts chain fetches; only the latest may write the page. */
  private chainRequest = 0;
  /** A silent poll is awaiting its reply: it sets no `chainLoading`, so it needs its own guard. */
  private pollInFlight = false;

  availableExpirations = signal<number[]>([]);
  selectedExpiration = signal<number | null>(null);

  underlying = signal<SnapshotUnderlyingResult | null>(null);
  allContracts = signal<SnapshotContractResult[]>([]);
  stockSnapshot = signal<StockTickerSnapshot | null>(null);

  readonly strikeRangeOptions = [5, 10, 15, 25] as const;
  strikeRange = signal<number>(15);
  showAllStrikes = signal(false);

  density = signal<ChainDensity>(this.readDensityFromStorage());

  spotPrice = computed(() => {
    const snap = this.stockSnapshot();
    if (snap?.day?.close != null && snap.day.close > 0) return snap.day.close;
    return this.underlying()?.price ?? 0;
  });

  spotChange = computed(() => this.underlying()?.change ?? 0);
  spotChangePct = computed(() => this.underlying()?.changePercent ?? 0);

  daysToExpiry = computed(() => {
    const exp = this.selectedExpiration();
    if (!exp) return 0;
    // The anchor instant from the wire — never a 16:00 built in the browser's
    // own zone (#2934).
    return Math.max(Math.ceil((exp - Date.now()) / 86_400_000), 0);
  });

  expirationLabel = computed(() => {
    const exp = this.selectedExpiration();
    // The anchor read in New York names its own date — the same expiry copy
    // as every other research page (#2915).
    if (!exp) return '';
    return formatTimestampDisplay(exp, { mode: 'date-et', granularity: 'short-date' });
  });

  rows = computed<ChainRow[]>(() => {
    const contracts = this.allContracts();
    const price = this.spotPrice();
    const range = this.strikeRange();
    const showAll = this.showAllStrikes();

    if (contracts.length === 0) return [];

    const callMap = new Map<number, SnapshotContractResult>();
    const putMap = new Map<number, SnapshotContractResult>();
    const strikeSet = new Set<number>();

    for (const c of contracts) {
      if (c.strikePrice == null) continue;
      strikeSet.add(c.strikePrice);
      if (c.contractType === 'call') callMap.set(c.strikePrice, c);
      else if (c.contractType === 'put') putMap.set(c.strikePrice, c);
    }

    const strikes = [...strikeSet].sort((a, b) => a - b);

    let atmStrike: number | null = null;
    if (price > 0 && strikes.length > 0) {
      let minDist = Infinity;
      for (const s of strikes) {
        const dist = Math.abs(s - price);
        if (dist < minDist) {
          atmStrike = s;
          minDist = dist;
        }
      }
    }

    let visibleStrikes = strikes;
    if (!showAll && atmStrike != null) {
      const atmIdx = strikes.indexOf(atmStrike);
      if (atmIdx !== -1) {
        const start = Math.max(0, atmIdx - range);
        const end = Math.min(strikes.length, atmIdx + range + 1);
        visibleStrikes = strikes.slice(start, end);
      }
    }

    let maxCallVol = 0;
    let maxPutVol = 0;
    for (const s of visibleStrikes) {
      const cv = callMap.get(s)?.day?.volume ?? 0;
      const pv = putMap.get(s)?.day?.volume ?? 0;
      if (cv > maxCallVol) maxCallVol = cv;
      if (pv > maxPutVol) maxPutVol = pv;
    }

    return visibleStrikes.map<ChainRow>(strike => {
      const call = callMap.get(strike) ?? null;
      const put = putMap.get(strike) ?? null;
      const isAtm = strike === atmStrike;
      const callIv = call?.impliedVolatility ?? 0;
      const putIv = put?.impliedVolatility ?? 0;
      const centerIvVal = callIv > 0 && putIv > 0 ? (callIv + putIv) / 2 : (callIv || putIv);

      return {
        strike,
        strikeFormatted: strike.toFixed(2),
        isAtm,
        itmCall: price > 0 && strike < price && !isAtm,
        itmPut: price > 0 && strike > price && !isAtm,
        centerIv: centerIvVal > 0 ? (centerIvVal * 100).toFixed(2) + '%' : EM_DASH,

        callLast: this.resolveLast(call),
        callBid: this.fmtPrice(call?.lastQuote?.bid),
        callAsk: this.fmtPrice(call?.lastQuote?.ask),
        callSpread: this.fmtSpread(call?.lastQuote?.bid, call?.lastQuote?.ask),
        callDelta: this.fmtGreek(call?.greeks?.delta),
        callGamma: this.fmtGreek(call?.greeks?.gamma),
        callTheta: this.fmtGreek(call?.greeks?.theta),
        callVega: this.fmtGreek(call?.greeks?.vega),
        callVolume: this.fmtVolume(call?.day?.volume),
        callVolumeBarWidth: this.barWidth(call?.day?.volume, maxCallVol),
        callOI: this.fmtVolume(call?.openInterest),

        putLast: this.resolveLast(put),
        putBid: this.fmtPrice(put?.lastQuote?.bid),
        putAsk: this.fmtPrice(put?.lastQuote?.ask),
        putSpread: this.fmtSpread(put?.lastQuote?.bid, put?.lastQuote?.ask),
        putDelta: this.fmtGreek(put?.greeks?.delta),
        putGamma: this.fmtGreek(put?.greeks?.gamma),
        putTheta: this.fmtGreek(put?.greeks?.theta),
        putVega: this.fmtGreek(put?.greeks?.vega),
        putVolume: this.fmtVolume(put?.day?.volume),
        putVolumeBarWidth: this.barWidth(put?.day?.volume, maxPutVol),
        putOI: this.fmtVolume(put?.openInterest),
      };
    });
  });

  spotChangeClass = computed(() => {
    const c = this.spotChange();
    if (c > 0) return 'pos';
    if (c < 0) return 'neg';
    return 'flat';
  });

  ngOnInit(): void {
    void this.fetchExpirations();

    // Poll silently — transient backend/network failures must NOT latch the
    // poller off (we never expose a manual retry button), so we don't gate
    // on `error()`. Silent fetches don't write to the error signal, and a
    // subsequent successful fetch clears any stale error from the initial
    // load.
    interval(POLL_INTERVAL_MS)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => {
        if (
          typeof document !== 'undefined' &&
          document.visibilityState === 'hidden'
        ) return;
        if (this.chainLoading() || this.expirationsLoading()) return;
        if (!this.selectedExpiration()) return;
        void this.fetchChainSnapshot(true);
      });
  }


  async fetchExpirations(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    // A late reply for a ticker the user has since left must not write this one's page.
    const request = ++this.expirationsRequest;
    this.expirationsLoading.set(true);
    this.error.set(null);
    this.availableExpirations.set([]);
    this.selectedExpiration.set(null);
    this.underlying.set(null);
    this.allContracts.set([]);
    this.stockSnapshot.set(null);
    this.chainAsOfMs.set(null);
    this.refreshError.set(null);

    try {
      const [expirations, snapshotResult] = await Promise.all([
        firstValueFrom(this.market.getOptionsExpirations(t)),
        firstValueFrom(this.market.getStockSnapshot(t)).catch(() => null),
      ]);

      if (request !== this.expirationsRequest) return;

      if (snapshotResult?.success && snapshotResult.snapshot) {
        this.stockSnapshot.set(snapshotResult.snapshot);
      }

      this.availableExpirations.set(expirations);

      if (expirations.length > 0) {
        const todayAnchor = optionExpiryAnchorMs(etIsoDate(Date.now()));
        const nearest = expirations.find(e => e >= todayAnchor) ?? expirations[0];
        this.selectedExpiration.set(nearest);
        await this.fetchChainSnapshot(false);
      } else {
        this.initialLoad.set(false);
      }
    } catch (err) {
      if (request === this.expirationsRequest) {
        this.error.set(errorMessage(err));
        this.initialLoad.set(false);
      }
    } finally {
      // Only the latest fetch ends the loading state it started.
      if (request === this.expirationsRequest) this.expirationsLoading.set(false);
    }
  }

  async onExpirationSelected(expirationMs: number): Promise<void> {
    if (expirationMs === this.selectedExpiration()) return;
    this.selectedExpiration.set(expirationMs);
    // The previous expiry's quotes must not stay under the new selection if
    // this fetch fails.
    this.allContracts.set([]);
    this.chainAsOfMs.set(null);
    this.refreshError.set(null);
    await this.fetchChainSnapshot(false);
  }

  async fetchChainSnapshot(silent: boolean): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    const exp = this.selectedExpiration();
    if (!t || !exp) return;
    if (silent && this.pollInFlight) return;

    const request = ++this.chainRequest;
    // A late reply for a ticker or expiration the user has since left must not
    // overwrite the chain now on screen.
    const current = () => request === this.chainRequest
      && this.selectedExpiration() === exp
      && this.ticker().trim().toUpperCase() === t;
    // A silent poll's failure is no banner: it marks the quotes on screen stale.
    const fail = (reason: string) => (silent ? this.refreshError : this.error).set(reason);

    this.pollInFlight = silent;
    if (!silent) this.chainLoading.set(true);
    if (!silent) this.error.set(null);

    try {
      const result = await firstValueFrom(this.market.getOptionsChainSnapshot(t, exp));
      if (!current()) return;
      if (!result.success) {
        fail(result.error ?? 'Failed to fetch options snapshot');
        return;
      }
      this.underlying.set(result.underlying);
      this.allContracts.set(result.contracts);
      this.chainAsOfMs.set(Date.now());
      this.refreshError.set(null);
      // Clear any stale error from a prior failed load now that fresh
      // data is in hand — keeps banner state in sync after a recovery.
      this.error.set(null);
    } catch (err) {
      if (current()) fail(errorMessage(err));
    } finally {
      // Only the latest fetch ends the state it started.
      if (request === this.chainRequest) {
        this.pollInFlight = false;
        this.chainLoading.set(false);
        this.initialLoad.set(false);
      }
    }
  }

  setStrikeRange(n: number): void {
    this.strikeRange.set(n);
    this.showAllStrikes.set(false);
  }

  toggleShowAll(): void {
    this.showAllStrikes.update(v => !v);
  }

  setDensity(d: ChainDensity): void {
    this.density.set(d);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(DENSITY_STORAGE_KEY, d);
    }
  }

  addLeg(strike: number, right: 'CALL' | 'PUT', action: 'LONG' | 'SHORT'): void {
    // stub: future integration with options strategy builder
    void strike; void right; void action;
  }

  trackRow = (_: number, r: ChainRow): number => r.strike;

  private readDensityFromStorage(): ChainDensity {
    if (typeof localStorage === 'undefined') return 'quick';
    const v = localStorage.getItem(DENSITY_STORAGE_KEY);
    if (v === 'quick' || v === 'greeks' || v === 'flow') return v;
    return 'quick';
  }

  private fmtPrice(v: number | null | undefined): string {
    return v != null && v > 0 ? v.toFixed(2) : EM_DASH;
  }

  private resolveLast(c: SnapshotContractResult | null): string {
    if (!c) return EM_DASH;
    if (c.day?.close != null && c.day.close > 0) return c.day.close.toFixed(2);
    if (c.lastTrade?.price != null && c.lastTrade.price > 0) return c.lastTrade.price.toFixed(2);
    if (c.lastQuote?.midpoint != null && c.lastQuote.midpoint > 0) return c.lastQuote.midpoint.toFixed(2);
    const bid = c.lastQuote?.bid ?? 0;
    const ask = c.lastQuote?.ask ?? 0;
    if (bid > 0 && ask > 0) return ((bid + ask) / 2).toFixed(2);
    return EM_DASH;
  }

  private fmtSpread(bid: number | null | undefined, ask: number | null | undefined): string {
    if (bid == null || ask == null || bid <= 0 || ask <= 0 || ask < bid) return EM_DASH;
    return (ask - bid).toFixed(2);
  }

  private fmtGreek(v: number | null | undefined): string {
    return v != null ? v.toFixed(3) : EM_DASH;
  }

  private fmtVolume(v: number | null | undefined): string {
    if (v == null || v === 0) return EM_DASH;
    if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M';
    if (v >= 1_000) return (v / 1_000).toFixed(1) + 'K';
    return v.toLocaleString();
  }

  private barWidth(volume: number | null | undefined, max: number): number {
    if (!volume || !max) return 0;
    return Math.min(100, (volume / max) * 100);
  }
}
