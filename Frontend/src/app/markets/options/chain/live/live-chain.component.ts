import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked } from '@angular/core';

import { ExpirationRibbonComponent } from '../../../../components/options-chain-v2/expiration-ribbon/expiration-ribbon.component';
import { BrokerHealthService } from '../../../../services/broker-health.service';
import { MarketDataFeedService } from '../../../../services/market-data-feed.service';
import { QuantLibService } from '../../../../services/quantlib.service';
import { optionExpiryDate } from '../../../../shared/option-contract/option-expiry-date';
import { HealthNoticeComponent } from '../../../../shared/scoped-health/health-notice.component';
import { HEALTH_NEEDS } from '../../../../shared/scoped-health/scoped-health';
import type { WorkspaceStateContent } from '../../../../shared/workspace/workspace-state';
import { nearestExpiry } from '../chain-expiry';
import type { ChainPresentation } from '../chain-presentation';
import { ChainViewComponent } from '../chain-view.component';
import { LiveChainFeed } from './live-chain-feed.service';
import { livePresentation } from './live-chain';
import { LiveEngineReads } from './live-engine-reads.service';
import {
  connectionLine,
  entitlementLine,
  failureWords,
  liveGate,
  subscriptionLine,
  type FeedEvidence,
} from './live-feed-status';
import { LiveStrikePickerComponent } from './live-strike-picker.component';

/** The band the Chain (Live) page selected around the spot: every qualifiable strike within this many dollars. */
const ATM_BAND_DOLLARS = 20;
const strikesKey = (symbol: string, expiryMs: number): string => `${symbol.toUpperCase()}:${expiryMs}`;

/** Picking through expiries fast must not ask IBKR to qualify every strike of each one. */
const STRIKES_DELAY_MS = 250;

/**
 * Now · Live IBKR: the retained read-only IBKR feed on the instrument's Options chain.
 *
 * It reads the data plane's IBKR session through the one `BrokerHealthService`, lists the
 * underlying's expirations and qualifiable strikes (read-only requests), and, only when the operator
 * presses Start, holds one subscription for the chosen strikes through the page's one
 * `LiveChainFeed`. Entering the mode, picking an expiry or picking strikes subscribes to nothing.
 *
 * When the data plane holds no IBKR session the page says Live is unavailable and gives the
 * reason the data plane gave; it never falls back to another provider. Changing the underlying or
 * the expiry, leaving the mode, or losing the session releases the subscription and forgets the
 * chain, so a frame or an engine reply that arrives late lands in nothing.
 */
@Component({
  selector: 'app-live-chain',
  imports: [ExpirationRibbonComponent, HealthNoticeComponent, LiveStrikePickerComponent, ChainViewComponent],
  providers: [LiveChainFeed, LiveEngineReads],
  templateUrl: './live-chain.component.html',
  styleUrl: './live-chain.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LiveChainComponent {
  private readonly health = inject(BrokerHealthService);
  private readonly market = inject(MarketDataFeedService);
  private readonly quantlib = inject(QuantLibService);
  private readonly engine = inject(LiveEngineReads);
  protected readonly feed = inject(LiveChainFeed);
  /** The stream is the IBKR feed; its own reason stays where the controls are. */
  protected readonly healthNeeds = HEALTH_NEEDS.liveChain;

  readonly symbol = input.required<string>();
  /** The expiry the address names; `null` when it names none. */
  readonly expiryMs = input<number | null>(null);
  readonly expiryPicked = output<number>();
  /** The expiry the chain opened on because the address named none. */
  readonly expiryAdopted = output<number>();

  protected readonly bandDollars = ATM_BAND_DOLLARS;
  protected readonly expirations = signal<number[]>([]);
  protected readonly expirationsLoading = signal(false);
  private readonly expirationsRead = signal(false);
  protected readonly availableStrikes = signal<readonly number[]>([]);
  protected readonly selectedStrikes = signal<ReadonlySet<number>>(new Set());
  protected readonly strikesLoading = signal(false);
  private readonly setupError = signal<string | null>(null);
  /** Counts listing reads; only the latest may write the page. */
  private expirationsRequest = 0;
  /** Counts strike reads; only the latest may write the page, and leaving a chain ends the one in flight. */
  private strikesRequest = 0;
  /**
   * The strikes IBKR qualified for an (underlying, expiry) this page has read. Qualifying a chain's strikes
   * is hundreds of contract-details requests on the data plane's one shared IBKR session, so an expiry is
   * read once and shown again from here; a failed read is never kept, and Try again reads it anew.
   */
  private readonly strikeLists = new Map<string, readonly number[]>();

  private readonly connection = computed(() => {
    const failure = this.health.lastError();
    return { health: this.health.health(), failure: failure === null ? null : failureWords(failure) };
  });
  protected readonly gate = computed(() => liveGate(this.connection()));
  private readonly serving = computed(() => this.gate().kind === 'ready');

  private readonly nearest = computed(() => nearestExpiry(this.expirations(), Date.now()));
  /** The expiry on screen: the address's, else the nearest IBKR lists. */
  protected readonly selected = computed(() => this.expiryMs() ?? this.nearest());
  /** The expiry whose chain is read: the one on screen, once IBKR has said it lists it. */
  protected readonly chainExpiry = computed(() => {
    const selected = this.selected();
    return selected !== null && this.expirations().includes(selected) ? selected : null;
  });

  /** The snapshot of the chain on screen. One for another underlying or expiry is never it, whatever the stream delivered. */
  private readonly shown = computed(() => {
    const snapshot = this.feed.snapshot();
    return snapshot !== null && snapshot.symbol === this.symbol().toUpperCase() && snapshot.expiry_ms === this.chainExpiry() ? snapshot : null;
  });

  protected readonly chain = computed<ChainPresentation>(() => {
    const evidence: FeedEvidence = {
      subscription: this.feed.subscription(),
      state: this.feed.state(),
      streamError: this.feed.error(),
      live: this.feed.live(),
      snapshot: this.shown(),
      health: this.connection().health,
    };
    return livePresentation({
      symbol: this.symbol().toUpperCase(),
      expiryMs: this.chainExpiry(),
      snapshot: evidence.snapshot,
      serving: this.serving(),
      live: evidence.live,
      engine: this.quantlib.selectedEngine(),
      answers: this.engine.answers(),
      connection: connectionLine(this.connection()),
      entitlement: entitlementLine(evidence),
      subscription: subscriptionLine(evidence),
    });
  });

  /** Why there is no grid to draw, when there is none. */
  protected readonly blocking = computed<WorkspaceStateContent | null>(() => {
    const gate = this.gate();
    if (gate.kind === 'blocked') return gate.state;
    const symbol = this.symbol().toUpperCase();
    const setupError = this.setupError();
    if (setupError !== null) {
      return {
        kind: 'failed',
        happened: `IBKR could not list ${symbol}'s expirations or strikes.`,
        why: setupError,
        next: 'Try again, or pick another expiry. Nothing is subscribed.',
        actions: [{ id: 'retry', label: 'Try again' }],
      };
    }
    if (this.expirationsLoading() || !this.expirationsRead()) return { kind: 'checking', happened: `Reading ${symbol}'s option expirations from IBKR.` };
    if (this.expirations().length === 0) return { kind: 'unavailable', happened: `IBKR lists no option expirations for ${symbol}.` };
    const selected = this.selected();
    if (selected !== null && this.chainExpiry() === null) {
      return {
        kind: 'unavailable',
        happened: `The ${optionExpiryDate(selected)} expiry is not among IBKR's current expirations for ${symbol}.`,
        next: 'Pick one in the ribbon. None has been opened for you.',
      };
    }
    if (this.shown() !== null) return null;
    switch (this.feed.state()) {
      case 'connecting':
      case 'open':
        return { kind: 'checking', happened: 'Waiting for the first snapshot.' };
      case 'error':
        return {
          kind: 'failed',
          happened: 'The chain stream stopped before a snapshot arrived.',
          why: this.feed.error() ?? 'The stream reported an error and gave no reason.',
          next: 'The stream is closed and does not reconnect by itself. Resume, or press Start again when the strikes are as you want them.',
        };
      case 'stopped':
        return { kind: 'idle', happened: 'Paused before a snapshot arrived.', next: 'Resume, or start again.' };
      case 'none':
        return {
          kind: 'idle',
          happened: 'Nothing is subscribed.',
          next: 'Pick the strikes to follow, then press Start. Each contract takes one of IBKR\'s shared market-data lines.',
        };
    }
  });

  protected readonly canStart = computed(() => this.serving() && this.chainExpiry() !== null && this.selectedStrikes().size > 0);
  /** The subscription is held but its stream is closed, by a pause or by an error: the operator can subscribe again. */
  protected readonly resumable = computed(() => this.feed.state() === 'stopped' || this.feed.state() === 'error');
  protected readonly canPause = computed(() => this.feed.state() !== 'none');

  /** Why the strikes within the band cannot be picked yet: the band is read around a price, and the only price is the stream's. */
  protected readonly bandUnavailable = computed(() => {
    if (this.availableStrikes().length === 0) return 'No strikes are listed to pick from.';
    return this.shown()?.underlying_price == null ? 'IBKR gives the underlying\'s price only once a stream is open, so the band is available after the first Start.' : null;
  });

  constructor() {
    this.health.start();

    // A different underlying, or the data plane's session coming or going, is a different chain: all of the last goes, and the listing is read again.
    effect(() => {
      const symbol = this.symbol();
      const serving = this.serving();
      untracked(() => {
        this.leaveChain();
        this.expirationsRequest++;
        this.expirations.set([]);
        this.expirationsRead.set(false);
        this.setupError.set(null);
        this.expirationsLoading.set(false);
        if (serving) void this.loadExpirations(symbol);
      });
    });

    // The address named no expiry, so the nearest IBKR lists is the one on screen: hand it up to be written.
    effect(() => {
      const nearest = this.nearest();
      if (this.expiryMs() === null && nearest !== null) untracked(() => this.expiryAdopted.emit(nearest));
    });

    // A different expiry is a different chain: the subscription is released, and its strikes are read.
    effect((onCleanup) => {
      const expiry = this.chainExpiry();
      const symbol = this.symbol();
      untracked(() => this.leaveChain());
      if (expiry === null) return;
      const read = this.strikeLists.get(strikesKey(symbol, expiry));
      if (read !== undefined) {
        untracked(() => this.availableStrikes.set(read));
        return;
      }
      const timer = setTimeout(() => void this.loadStrikes(symbol, expiry), STRIKES_DELAY_MS);
      onCleanup(() => clearTimeout(timer));
    });

    // Each snapshot of the chain on screen asks the engine about the contracts whose question has changed.
    effect(() => {
      const snapshot = this.shown();
      const live = this.feed.live();
      const engine = this.quantlib.selectedEngine();
      if (snapshot !== null) untracked(() => this.engine.offer(snapshot, { live, snapshot, engine }));
    });
  }

  protected toggleStrike(strike: number): void {
    this.selectedStrikes.update((current) => {
      const next = new Set(current);
      if (next.has(strike)) next.delete(strike);
      else next.add(strike);
      return next;
    });
  }

  protected selectBand(): void {
    const spot = this.shown()?.underlying_price;
    if (spot == null) return;
    this.selectedStrikes.set(new Set(this.availableStrikes().filter((strike) => Math.abs(strike - spot) <= ATM_BAND_DOLLARS)));
  }

  protected clearStrikes(): void {
    this.selectedStrikes.set(new Set());
  }

  protected start(): void {
    const expiryMs = this.chainExpiry();
    const strikes = [...this.selectedStrikes()];
    if (!this.serving() || expiryMs === null || strikes.length === 0) return;
    this.engine.reset();
    this.feed.start({ symbol: this.symbol(), expiryMs, strikes });
  }

  protected togglePause(): void {
    if (this.resumable()) this.feed.resume();
    else this.feed.pause();
  }

  /** Try again reads what failed: the expirations when they were not read, else the strikes of the expiry open. */
  protected onAction(id: string): void {
    if (id !== 'retry') return;
    const expiry = this.chainExpiry();
    if (this.expirationsRead() && expiry !== null) void this.loadStrikes(this.symbol(), expiry);
    else void this.loadExpirations(this.symbol());
  }

  /** Lets go of the chain on screen: the subscription, the engine's questions and answers, and the strikes read for it. */
  private leaveChain(): void {
    this.feed.release();
    this.engine.reset();
    this.strikesRequest++;
    this.availableStrikes.set([]);
    this.selectedStrikes.set(new Set());
    this.strikesLoading.set(false);
  }

  private async loadExpirations(symbol: string): Promise<void> {
    const request = ++this.expirationsRequest;
    this.expirationsLoading.set(true);
    this.setupError.set(null);
    try {
      const listed = await this.market.expirations(symbol);
      if (request !== this.expirationsRequest) return;
      this.expirations.set([...listed.expirations_ms].sort((a, b) => a - b));
      this.expirationsRead.set(true);
    } catch (error) {
      if (request === this.expirationsRequest) this.setupError.set(failureWords(error));
    } finally {
      if (request === this.expirationsRequest) this.expirationsLoading.set(false);
    }
  }

  private async loadStrikes(symbol: string, expiryMs: number): Promise<void> {
    const request = ++this.strikesRequest;
    this.strikesLoading.set(true);
    // A failure of an earlier read is not this one's: it is said again only if this read fails too.
    this.setupError.set(null);
    try {
      const listed = await this.market.strikes(symbol, expiryMs);
      const strikes = [...listed.strikes].sort((a, b) => a - b);
      // A listing is true of its (underlying, expiry) whichever chain is on screen when it arrives.
      this.strikeLists.set(strikesKey(symbol, expiryMs), strikes);
      if (request !== this.strikesRequest) return;
      this.availableStrikes.set(strikes);
      this.selectedStrikes.set(new Set());
    } catch (error) {
      if (request === this.strikesRequest) {
        this.availableStrikes.set([]);
        this.setupError.set(failureWords(error));
      }
    } finally {
      if (request === this.strikesRequest) this.strikesLoading.set(false);
    }
  }
}
