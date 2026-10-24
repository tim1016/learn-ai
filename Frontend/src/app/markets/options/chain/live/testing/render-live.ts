import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal, type Provider, type WritableSignal } from '@angular/core';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import type { Mock, vi as Vi } from 'vitest';

import type { IbkrConnectionHealth, IbkrStrikeList, ExpirationsResponse } from '../../../../../api/broker-models';
import { BrokerHealthService } from '../../../../../services/broker-health.service';
import { MarketDataFeedService } from '../../../../../services/market-data-feed.service';
import type { QuantLibEngine, QuantLibPriceResult } from '../../../../../services/market-data.types';
import { QuantLibService } from '../../../../../services/quantlib.service';
import { connectedHealth, engineReply, fakeIbkrFeed, FAR, NEAR } from './fake-ibkr';
import { LiveHostComponent } from './live-host.component';

/** The `vi` of the spec that calls: a shared helper never imports vitest itself. */
type Spy = Pick<typeof Vi, 'fn'>;

type PriceRequest = Parameters<QuantLibService['priceOption']>[0];

export interface LiveWorld {
  readonly health: WritableSignal<IbkrConnectionHealth | null>;
  readonly healthFailure: WritableSignal<unknown>;
  readonly engine: WritableSignal<QuantLibEngine>;
  readonly expirations: Mock<(symbol: string) => Promise<ExpirationsResponse>>;
  readonly strikes: Mock<(symbol: string, expiryMs: number) => Promise<IbkrStrikeList>>;
  readonly price: Mock<(request: PriceRequest) => Promise<QuantLibPriceResult>>;
  readonly feed: ReturnType<typeof fakeIbkrFeed>;
}

/** Unless a spec says otherwise: IBKR is connected, two expiries are listed, three strikes qualify, and the engine agrees with IBKR's delta. */
export function liveWorld(spy: Spy, changes: Partial<LiveWorld> = {}): LiveWorld {
  return {
    health: signal<IbkrConnectionHealth | null>(connectedHealth()),
    healthFailure: signal<unknown>(null),
    engine: signal<QuantLibEngine>('analytic_bs'),
    expirations: spy.fn(async (symbol: string) => ({ symbol, expirations_ms: [NEAR, FAR] })),
    strikes: spy.fn(async (symbol: string, expiryMs: number) => ({ symbol, expiry_ms: expiryMs, strikes: [499, 500, 501], fetched_at_ms: 0 })),
    price: spy.fn(async (request: PriceRequest) => engineReply(request.optionType === 'call' ? 0.4 : -0.4)),
    feed: fakeIbkrFeed(),
    ...changes,
  };
}

/**
 * Renders the live chain behind a host that plays the address, with IBKR, the engine and the stream all faked.
 * The scoped-health notice above the controls reads over HTTP, which no spec answers unless it passes `more`.
 */
export async function renderLive(spy: Spy, world: LiveWorld, start: { symbol?: string; expiryMs?: number } = {}, more: readonly Provider[] = []) {
  const view = await render(LiveHostComponent, {
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: BrokerHealthService, useValue: { health: world.health, lastError: world.healthFailure, start: spy.fn() } },
      { provide: MarketDataFeedService, useValue: { expirations: world.expirations, strikes: world.strikes } },
      { provide: QuantLibService, useValue: { selectedEngine: world.engine, priceOption: world.price } },
      world.feed.provide,
      ...more,
    ],
  });
  const host = view.fixture.componentInstance;
  if (start.symbol !== undefined) host.symbol.set(start.symbol);
  host.expiryMs.set(start.expiryMs ?? NEAR);
  view.fixture.detectChanges();
  await view.fixture.whenStable();
  return { ...view, host };
}

/** Picks the strikes by their chips, then presses Start, as an operator does. */
export async function startWith(...strikes: readonly number[]): Promise<void> {
  const user = userEvent.setup();
  for (const strike of strikes) await user.click(await screen.findByRole('button', { name: String(strike), pressed: false }));
  const start = await screen.findByRole('button', { name: 'Start' });
  await waitFor(() => {
    if (start.hasAttribute('disabled')) throw new Error('Start is still disabled.');
  });
  await user.click(start);
}
