import { computed, signal, type Signal, type WritableSignal } from '@angular/core';

import type { IbkrChainSnapshot, IbkrConnectionHealth, IbkrOptionQuote, OptionRight } from '../../../../../api/broker-models';
import type { SseStatus, SseStream } from '../../../../../services/broker-sse';
import type { QuantLibPriceResult } from '../../../../../services/market-data.types';
import { optionExpiryAnchorMs } from '../../../../../shared/option-contract/option-expiry-date';
import { LIVE_CHAIN_STREAM, type LiveChainStreamOpener } from '../live-chain-feed.service';

/*
 * A fake IBKR for the live chain's specs. No spec or preview ever connects to a Gateway: the chain
 * stream, the data plane's health and the engine are all stood in for, here and at the DI seam.
 */

export const NEAR = optionExpiryAnchorMs('2099-01-15');
export const FAR = optionExpiryAnchorMs('2099-02-19');
/** A snapshot clock well before `NEAR`'s date, so no fixture contract expires "today". */
export const AS_OF_MS = Date.UTC(2099, 0, 5, 15, 0, 0);

/** One option contract's tick, with the usual live values unless a spec changes them. */
export function ibkrQuote(strike: number, right: OptionRight, changes: Partial<IbkrOptionQuote> = {}): IbkrOptionQuote {
  return {
    symbol: 'SPY',
    expiry_ms: NEAR,
    strike,
    right,
    bid: 5,
    ask: 5.2,
    last: 5.1,
    bid_size: 10,
    ask_size: 10,
    iv: 0.2,
    delta: right === 'C' ? 0.4 : -0.4,
    gamma: 0.01,
    theta: -0.05,
    vega: 0.1,
    underlying_price: 500,
    greeks_source: 'model',
    ts_ms: AS_OF_MS - 100,
    ...changes,
  };
}

/** The data plane's chain snapshot; by default a call and a put at 500 on `NEAR`'s expiry. */
export function ibkrFrame(changes: Partial<IbkrChainSnapshot> = {}): IbkrChainSnapshot {
  return {
    symbol: 'SPY',
    expiry_ms: NEAR,
    underlying_price: 500,
    quotes: [ibkrQuote(500, 'C'), ibkrQuote(500, 'P')],
    as_of_ms: AS_OF_MS,
    ...changes,
  };
}

/** An IBKR quote with nothing in it: the data plane's answer for a contract that has sent no tick. */
export function silentQuote(strike: number, right: OptionRight): IbkrOptionQuote {
  return ibkrQuote(strike, right, { bid: null, ask: null, last: null, iv: null, delta: null, gamma: null, theta: null, vega: null, underlying_price: null, greeks_source: 'none' });
}

export function connectedHealth(changes: Partial<IbkrConnectionHealth> = {}): IbkrConnectionHealth {
  return {
    mode: 'paper',
    host: 'localhost',
    port: 4002,
    client_id: 7,
    connected: true,
    is_paper: true,
    fetched_at_ms: AS_OF_MS,
    connection_state: 'connected',
    last_transition_ms: AS_OF_MS,
    condition: {
      code: 'DATA_PLANE_BROKER_CONNECTED',
      severity: 'ok',
      title: 'Data-plane paper session connected',
      summary: 'The FastAPI data-plane IBKR client is connected. Market data can stream for this session.',
    },
    ...changes,
  };
}

/** The data plane with no Gateway session, as its health answers when no client exists. */
export function disconnectedHealth(changes: Partial<IbkrConnectionHealth> = {}): IbkrConnectionHealth {
  return connectedHealth({
    connected: false,
    is_paper: null,
    connection_state: 'disconnected',
    condition: {
      code: 'DATA_PLANE_BROKER_DISCONNECTED',
      severity: 'warning',
      title: 'Data-plane broker session disconnected',
      summary: 'The FastAPI data-plane IBKR client is disconnected. IB Gateway/TWS may still be logged in, but market data cannot stream until this app session connects.',
      remediation: 'Use the IBKR Connect control to establish the data-plane session.',
    },
    ...changes,
  });
}

/** The engine's answer for one contract. */
export function engineReply(delta: number, changes: Partial<QuantLibPriceResult> = {}): QuantLibPriceResult {
  return { success: true, engine: 'analytic_bs', price: 5, delta, gamma: 0.01, theta: -0.05, vega: 0.1, rho: 0.02, ...changes };
}

/**
 * One open chain stream as the page sees it. A spec drives it with `emit` and `fail`, and can
 * keep emitting after the page has closed it, as a transport that delivers a late event would:
 * the page must not show it.
 */
export class FakeChainStream implements SseStream<IbkrChainSnapshot> {
  private readonly frames = signal<readonly IbkrChainSnapshot[]>([]);
  private readonly state: WritableSignal<SseStatus> = signal('connecting');
  private readonly failure = signal<string | null>(null);
  private readonly activity = signal<number | null>(null);

  readonly data: Signal<readonly IbkrChainSnapshot[]> = this.frames.asReadonly();
  readonly latest: Signal<IbkrChainSnapshot | null> = computed(() => this.frames().at(-1) ?? null);
  readonly status: Signal<SseStatus> = this.state.asReadonly();
  readonly lastError: Signal<string | null> = this.failure.asReadonly();
  readonly lastActivityAtMs: Signal<number | null> = this.activity.asReadonly();

  /** How many times the page closed it: each is a release of every market-data line it held. */
  closes = 0;

  constructor(readonly url: string) {}

  get strikes(): number[] {
    return new URL(this.url, 'http://localhost').searchParams.getAll('strikes').map(Number);
  }

  get symbol(): string {
    return decodeURIComponent(this.url.split('?')[0].split('/').at(-1) ?? '');
  }

  emit(frame: IbkrChainSnapshot, receivedAtMs = Date.now()): void {
    this.frames.set([frame]);
    this.activity.set(receivedAtMs);
    if (this.state() !== 'closed') this.state.set('open');
  }

  fail(message: string | null): void {
    this.failure.set(message);
    this.state.set('error');
  }

  clear(): void {
    this.frames.set([]);
  }

  close(): void {
    this.closes++;
    this.state.set('closed');
  }
}

/** The streams the page opened, in order, and the provider that makes the page open these and no real one. */
export function fakeIbkrFeed() {
  const streams: FakeChainStream[] = [];
  const opener: LiveChainStreamOpener = (url) => {
    const stream = new FakeChainStream(url);
    streams.push(stream);
    return stream;
  };
  return { streams, opener, provide: { provide: LIVE_CHAIN_STREAM, useValue: opener } };
}
