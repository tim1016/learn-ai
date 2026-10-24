import { DestroyRef, Injectable, InjectionToken, Injector, computed, effect, inject, runInInjectionContext, signal, untracked } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { interval } from 'rxjs';

import type { IbkrChainSnapshot } from '../../../../api/broker-models';
import { brokerSse, type SseStream } from '../../../../services/broker-sse';
import { FRAME_STALL_MS } from './live-cross-check';

/** The chain stream asks for a snapshot every half second; the data plane takes 50 ms to 5 s. */
const DEBOUNCE_MS = 500;
const CLOCK_TICK_MS = 1_000;

/** Opens the data plane's option-chain stream. The injector owns the stream's lifetime if the page forgets it. */
export type LiveChainStreamOpener = (url: string, injector: Injector) => SseStream<IbkrChainSnapshot>;

/**
 * The one way a live chain stream is opened: the retained read-only IBKR feed's SSE helper. A spec
 * provides a fake here, so no test or preview ever reaches a Gateway.
 */
export const LIVE_CHAIN_STREAM = new InjectionToken<LiveChainStreamOpener>('LIVE_CHAIN_STREAM', {
  providedIn: 'root',
  factory: () => (url, injector) => runInInjectionContext(injector, () => brokerSse<IbkrChainSnapshot>(url, 'chain', { maxBuffer: 1 })),
});

export function chainStreamUrl(symbol: string, expiryMs: number, strikes: readonly number[]): string {
  const params = new URLSearchParams();
  params.set('expiry_ms', String(expiryMs));
  for (const strike of strikes) params.append('strikes', String(strike));
  params.set('debounce_ms', String(DEBOUNCE_MS));
  return `/api/broker/option-chain/${encodeURIComponent(symbol)}?${params.toString()}`;
}

/** What a subscription was opened for. */
export interface LiveSubscription {
  readonly symbol: string;
  readonly expiryMs: number;
  readonly strikes: readonly number[];
}

export type LiveFeedState = 'none' | 'connecting' | 'open' | 'error' | 'stopped';

interface Held extends LiveSubscription {
  /** The open stream; null once it has been paused, or stopped by an error. */
  readonly stream: SseStream<IbkrChainSnapshot> | null;
  /** The last snapshot of a paused or failed stream, kept so it can be shown as stale. */
  readonly last: IbkrChainSnapshot | null;
  /** Set when the stream reported an error and the feed closed it: the reason it gave, or null when it gave none. */
  readonly failure: { readonly reason: string | null } | null;
}

/**
 * The page's one live subscription: the single owner of the option-chain stream, provided by the
 * chain page so every part of it reads the same stream and none opens its own.
 *
 * A subscription is held for one (underlying, expiry, strikes) and nothing else. Starting another,
 * releasing, or leaving the page closes the stream, which makes the data plane cancel every
 * market-data line it opened. A frame is shown only if it comes from the subscription now held and
 * names that underlying and expiry, so an event that arrives after a switch lands nowhere: the
 * snapshot is read from the held stream alone, never copied out of an old one.
 *
 * A stream that reports an error is closed here, not left to the browser: an `EventSource` that is
 * still open reconnects by itself, and every reconnect would qualify the picked contracts and
 * subscribe to their market-data lines again on the data plane's one shared IBKR session. The
 * subscription then reads as stopped with the stream's reason, keeps its last snapshot as stale,
 * and subscribes again only when the operator presses Resume or Start.
 */
@Injectable()
export class LiveChainFeed {
  private readonly open = inject(LIVE_CHAIN_STREAM);
  private readonly injector = inject(Injector);
  private readonly held = signal<Held | null>(null);
  private readonly nowMs = signal(Date.now());

  /** What is subscribed, or null when nothing is. */
  readonly subscription = computed<LiveSubscription | null>(() => {
    const held = this.held();
    return held === null ? null : { symbol: held.symbol, expiryMs: held.expiryMs, strikes: held.strikes };
  });

  readonly state = computed<LiveFeedState>(() => {
    const held = this.held();
    if (held === null) return 'none';
    if (held.stream === null) return held.failure === null ? 'stopped' : 'error';
    const status = held.stream.status();
    return status === 'open' ? 'open' : status === 'error' ? 'error' : 'connecting';
  });

  /** The reason the stream gave for stopping, when it gave one. */
  readonly error = computed<string | null>(() => {
    const held = this.held();
    return held === null ? null : (held.failure?.reason ?? held.stream?.lastError() ?? null);
  });

  /** The latest snapshot of the subscription now held: a paused one keeps its last, and a frame for another chain is never one. */
  readonly snapshot = computed<IbkrChainSnapshot | null>(() => {
    const held = this.held();
    if (held === null) return null;
    const frame = held.stream === null ? held.last : held.stream.latest();
    return frame !== null && frame.symbol === held.symbol && frame.expiry_ms === held.expiryMs ? frame : null;
  });

  /** When the latest frame arrived in this browser (`int64 ms UTC`); null before the first. */
  readonly lastFrameAtMs = computed<number | null>(() => this.held()?.stream?.lastActivityAtMs() ?? null);

  /** The stream is open and has delivered a snapshot within the stall limit: what is shown is current. */
  readonly live = computed<boolean>(() => {
    if (this.state() !== 'open' || this.snapshot() === null) return false;
    const last = this.lastFrameAtMs();
    return last !== null && this.nowMs() - last <= FRAME_STALL_MS;
  });

  constructor() {
    const destroyRef = inject(DestroyRef);
    destroyRef.onDestroy(() => this.release());
    interval(CLOCK_TICK_MS).pipe(takeUntilDestroyed(destroyRef)).subscribe(() => this.nowMs.set(Date.now()));
    // An error ends the stream for good: the browser would otherwise reconnect and subscribe again by itself.
    effect(() => {
      const held = this.held();
      if (held?.stream?.status() === 'error') untracked(() => this.stopOnError(held));
    });
  }

  /** Subscribes to one chain, releasing whatever was subscribed before it. */
  start(subscription: LiveSubscription): void {
    this.release();
    const strikes = [...subscription.strikes].sort((a, b) => a - b);
    const symbol = subscription.symbol.toUpperCase();
    const stream = this.open(chainStreamUrl(symbol, subscription.expiryMs, strikes), this.injector);
    this.held.set({ symbol, expiryMs: subscription.expiryMs, strikes, stream, last: null, failure: null });
  }

  /** Pauses: closes the stream, which releases every line, and keeps the last snapshot to show as stale. */
  pause(): void {
    const held = this.held();
    if (held === null || held.stream === null) return;
    const last = this.snapshot();
    held.stream.close();
    this.held.set({ ...held, stream: null, last, failure: null });
  }

  /** Subscribes again to the chain held, after a pause or an error. It is the operator's call and never made by the feed. */
  resume(): void {
    const held = this.held();
    if (held !== null && held.stream === null) this.start(held);
  }

  /** Closes a stream that reported an error, keeping its reason and its last snapshot (to be shown as stale). */
  private stopOnError(held: Held): void {
    if (this.held() !== held || held.stream === null) return;
    const last = this.snapshot();
    const reason = held.stream.lastError();
    held.stream.close();
    this.held.set({ ...held, stream: null, last, failure: { reason } });
  }

  /** Lets go of the subscription: the stream is closed and nothing of it is kept. */
  release(): void {
    this.held()?.stream?.close();
    this.held.set(null);
  }
}
