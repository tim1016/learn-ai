import { ChangeDetectionStrategy, Component, DestroyRef, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { firstValueFrom, interval } from 'rxjs';

import { ExpirationRibbonComponent } from '../../../../components/options-chain-v2/expiration-ribbon/expiration-ribbon.component';
import { MarketDataService } from '../../../../services/market-data.service';
import type {
  SnapshotContractResult,
  SnapshotUnderlyingResult,
  StockTickerSnapshot,
} from '../../../../services/market-data.types';
import { etIsoDate } from '../../../../shared/date/et-midnight';
import { errorMessage } from '../../../../shared/errors/error-message';
import { optionExpiryAnchorMs } from '../../../../shared/option-contract/option-expiry-date';
import { formatTimestampDisplay } from '../../../../shared/timestamp';
import { WorkspaceStateBlockComponent } from '../../../../shared/workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../../../../shared/workspace/workspace-state';
import type { ChainPresentation } from '../chain-presentation';
import { ChainViewComponent } from '../chain-view.component';
import { snapshotPresentation, type ChainDensity } from './snapshot-chain';

/** The saved density keeps the key the Options Lab chain wrote, so the owner's choice survives the move. */
const DENSITY_STORAGE_KEY = 'optionsLab.chain.density';
const POLL_INTERVAL_MS = 5_000;
const STRIKE_RANGES = [5, 10, 15, 25] as const;
const DENSITIES: readonly ChainDensity[] = ['quick', 'greeks', 'flow'];

/** A saved choice that is not one of the three is the default: it is a convenience, never a requirement. */
function savedDensity(): ChainDensity {
  if (typeof localStorage === 'undefined') return 'quick';
  const saved = localStorage.getItem(DENSITY_STORAGE_KEY);
  return DENSITIES.find((density) => density === saved) ?? 'quick';
}

/**
 * Now · Snapshot: the provider's current option-chain snapshot for the instrument's place.
 *
 * It owns the loading the Options Lab chain owned: the expirations, the stock's own move, the
 * chain of one expiry, and a silent refresh every few seconds while the page is visible. Only the
 * latest request of each kind may write the page, so a late answer for an instrument or expiry
 * the operator has left lands nowhere. The refresh stops with the component, so leaving this
 * mode (or the page) leaves no request running to answer into another chain.
 *
 * The expiry is the address's. With none, the nearest the provider lists is opened, said so in
 * the ribbon and handed up to be written to the address, so the contract is the same one under
 * any source. An expiry the provider does not list is said and replaced by nothing.
 */
@Component({
  selector: 'app-snapshot-chain',
  imports: [ExpirationRibbonComponent, ChainViewComponent, WorkspaceStateBlockComponent],
  templateUrl: './snapshot-chain.component.html',
  styleUrl: './snapshot-chain.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SnapshotChainComponent {
  private readonly market = inject(MarketDataService);

  readonly symbol = input.required<string>();
  /** The expiry the address names; `null` when it names none. */
  readonly expiryMs = input<number | null>(null);
  readonly expiryPicked = output<number>();
  /** The expiry the chain opened on because the address named none. */
  readonly expiryAdopted = output<number>();

  protected readonly strikeRanges = STRIKE_RANGES;
  protected readonly densities = DENSITIES;

  protected readonly expirationsLoading = signal(false);
  protected readonly chainLoading = signal(false);
  protected readonly error = signal<string | null>(null);
  /** When the chain on screen last arrived (`int64 ms UTC`); null before the first one. */
  private readonly receivedAtMs = signal<number | null>(null);
  /** Why the latest background refresh failed, so quotes older than it read as stale. */
  private readonly refreshError = signal<string | null>(null);
  protected readonly availableExpirations = signal<number[]>([]);
  /** Whether the expirations of this instrument have been read, whatever they held. */
  private readonly expirationsRead = signal(false);
  private readonly underlying = signal<SnapshotUnderlyingResult | null>(null);
  private readonly contracts = signal<readonly SnapshotContractResult[]>([]);
  private readonly stock = signal<StockTickerSnapshot | null>(null);

  protected readonly strikeRange = signal<number>(15);
  protected readonly showAllStrikes = signal(false);
  protected readonly density = signal<ChainDensity>(savedDensity());

  /** Counts expiration fetches; only the latest may write the page. */
  private expirationsRequest = 0;
  /** Counts chain fetches; only the latest may write the page. */
  private chainRequest = 0;
  /** A silent poll is awaiting its reply: it sets no `chainLoading`, so it needs its own guard. */
  private pollInFlight = false;

  /** The nearest expiry the provider lists, from today's New York date. */
  private readonly nearest = computed<number | null>(() => {
    const listed = this.availableExpirations();
    const today = optionExpiryAnchorMs(etIsoDate(Date.now()));
    return listed.find((expiry) => expiry >= today) ?? listed[0] ?? null;
  });
  /** The expiry on screen: the address's, else the nearest. */
  protected readonly selected = computed(() => this.expiryMs() ?? this.nearest());
  /** The expiry whose chain is read: the one on screen, once the provider has said it lists it. */
  private readonly chainExpiry = computed(() => {
    const selected = this.selected();
    return selected !== null && this.availableExpirations().includes(selected) ? selected : null;
  });

  protected readonly presentation = computed<ChainPresentation | null>(() => {
    const expiryMs = this.chainExpiry();
    const receivedAtMs = this.receivedAtMs();
    if (expiryMs === null || receivedAtMs === null) return null;
    return snapshotPresentation({
      symbol: this.symbol().toUpperCase(),
      expiryMs,
      contracts: this.contracts(),
      underlying: this.underlying(),
      stock: this.stock(),
      strikeRange: this.strikeRange(),
      showAll: this.showAllStrikes(),
      density: this.density(),
      receivedAtMs,
      refreshError: this.refreshError(),
      todayEt: etIsoDate(Date.now()),
    });
  });

  /** Why there is no chain to draw, when there is none. */
  protected readonly blocking = computed<WorkspaceStateContent | null>(() => {
    const error = this.error();
    if (error !== null) return { kind: 'failed', happened: 'The snapshot could not be read.', why: error };
    if (this.expirationsLoading() || !this.expirationsRead()) return { kind: 'checking', happened: `Reading ${this.symbol().toUpperCase()}'s option expirations.` };
    if (this.availableExpirations().length === 0) {
      return { kind: 'unavailable', happened: `The provider lists no option expirations for ${this.symbol().toUpperCase()}.` };
    }
    const selected = this.selected();
    if (selected !== null && this.chainExpiry() === null) {
      const date = formatTimestampDisplay(selected, { mode: 'date-et', granularity: 'date' });
      return {
        kind: 'unavailable',
        happened: `The ${date} expiry is not among the provider's current expirations.`,
        why: 'The snapshot lists the next six months of expirations.',
        next: 'Pick one in the ribbon. None has been opened for you.',
      };
    }
    return this.presentation() === null ? { kind: 'checking', happened: 'Reading the snapshot.' } : null;
  });

  /** A snapshot that holds no contract for this expiry is said, with the chain's facts above it. */
  protected readonly emptyState = computed<WorkspaceStateContent | null>(() =>
    this.contracts().length === 0 ? { kind: 'idle', happened: 'No options data available for this expiration.' } : null,
  );

  constructor() {
    // A different instrument is a different subject: read its expirations, and drop everything of the last.
    effect(() => {
      const symbol = this.symbol();
      untracked(() => void this.fetchExpirations(symbol));
    });
    // The chain opened on the nearest expiry because the address named none: hand it up to be written.
    effect(() => {
      const nearest = this.nearest();
      if (this.expiryMs() === null && nearest !== null) untracked(() => this.expiryAdopted.emit(nearest));
    });
    // A different expiry or instrument is a different chain: the last one's quotes go at once, and the new one is read.
    effect(() => {
      const expiry = this.chainExpiry();
      this.symbol();
      untracked(() => {
        this.contracts.set([]);
        this.underlying.set(null);
        this.receivedAtMs.set(null);
        this.refreshError.set(null);
        this.error.set(null);
        if (expiry !== null) void this.fetchChainSnapshot(false);
      });
    });

    // Poll silently: a transient failure must not latch the poller off, so it does not gate on `error()`.
    // A silent fetch writes no error of its own, and a later success clears any stale one.
    interval(POLL_INTERVAL_MS)
      .pipe(takeUntilDestroyed(inject(DestroyRef)))
      .subscribe(() => {
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
        if (this.chainLoading() || this.expirationsLoading() || this.chainExpiry() === null) return;
        void this.fetchChainSnapshot(true);
      });
  }

  private async fetchExpirations(symbol: string): Promise<void> {
    const ticker = symbol.trim().toUpperCase();
    if (ticker === '') return;

    // A late reply for an instrument the operator has since left must not write this one's page.
    const request = ++this.expirationsRequest;
    this.expirationsLoading.set(true);
    this.expirationsRead.set(false);
    this.availableExpirations.set([]);
    this.stock.set(null);
    this.error.set(null);

    try {
      const [expirations, stock] = await Promise.all([
        firstValueFrom(this.market.getOptionsExpirations(ticker)),
        firstValueFrom(this.market.getStockSnapshot(ticker)),
      ]);
      if (request !== this.expirationsRequest) return;
      if (stock.success && stock.snapshot) this.stock.set(stock.snapshot);
      this.availableExpirations.set(expirations);
      this.expirationsRead.set(true);
    } catch (error) {
      if (request === this.expirationsRequest) this.error.set(errorMessage(error));
    } finally {
      // Only the latest fetch ends the loading state it started.
      if (request === this.expirationsRequest) this.expirationsLoading.set(false);
    }
  }

  private async fetchChainSnapshot(silent: boolean): Promise<void> {
    const ticker = this.symbol().trim().toUpperCase();
    const expiry = this.chainExpiry();
    if (ticker === '' || expiry === null) return;
    if (silent && this.pollInFlight) return;

    const request = ++this.chainRequest;
    // A late reply for an instrument or expiry the operator has since left must not overwrite the chain now on screen.
    const current = () => request === this.chainRequest && this.chainExpiry() === expiry && this.symbol().trim().toUpperCase() === ticker;
    // A silent poll's failure is no banner: it marks the quotes on screen stale.
    const fail = (reason: string) => (silent ? this.refreshError : this.error).set(reason);

    this.pollInFlight = silent;
    if (!silent) {
      this.chainLoading.set(true);
      this.error.set(null);
    }

    try {
      const result = await firstValueFrom(this.market.getOptionsChainSnapshot(ticker, expiry));
      if (!current()) return;
      if (!result.success) {
        fail(result.error ?? 'Failed to fetch options snapshot');
        return;
      }
      this.underlying.set(result.underlying);
      this.contracts.set(result.contracts);
      this.receivedAtMs.set(Date.now());
      this.refreshError.set(null);
      // Fresh data is in hand: clear any stale error from a prior failed load.
      this.error.set(null);
    } catch (error) {
      if (current()) fail(errorMessage(error));
    } finally {
      // Only the latest fetch ends the state it started.
      if (request === this.chainRequest) {
        this.pollInFlight = false;
        this.chainLoading.set(false);
      }
    }
  }

  protected setStrikeRange(count: number): void {
    this.strikeRange.set(count);
    this.showAllStrikes.set(false);
  }

  protected toggleShowAll(): void {
    this.showAllStrikes.update((all) => !all);
  }

  protected setDensity(density: ChainDensity): void {
    this.density.set(density);
    if (typeof localStorage !== 'undefined') localStorage.setItem(DENSITY_STORAGE_KEY, density);
  }
}
