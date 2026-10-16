import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { expect, it, vi } from 'vitest';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { MarketDataFeedService } from '../../../services/market-data-feed.service';
import { QuantLibService } from '../../../services/quantlib.service';
import type { IbkrChainSnapshot, IbkrConnectionHealth } from '../../../api/broker-models';
import { BrokerOptionsChainComponent } from './broker-options-chain.component';

it('starts the health poll and renders the live chain controls once IBKR connects', async () => {
  const health = signal<IbkrConnectionHealth | null>(null);
  const start = vi.fn();
  const expirations = vi.fn().mockResolvedValue({ expirations_ms: [] });
  const { fixture } = await render(BrokerOptionsChainComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerHealthService, useValue: { health, bannerState: signal(null), start } },
      { provide: MarketDataFeedService, useValue: { expirations, strikes: vi.fn() } },
      { provide: QuantLibService, useValue: { priceOption: vi.fn() } },
    ],
  });

  expect(start).toHaveBeenCalledOnce();
  expect(screen.getByText(/Broker is disconnected/)).toBeTruthy();
  health.set({ connected: true, client_id: 1, connection_state: 'connected', fetched_at_ms: 0,
    host: 'localhost', last_transition_ms: 0, mode: 'paper', port: 4002 });
  fixture.detectChanges();
  await waitFor(() => expect(screen.getByText('Refresh expirations')).toBeTruthy());
  await waitFor(() => expect(expirations).toHaveBeenCalledOnce());

  fixture.componentInstance.paused.set(true);
  fixture.detectChanges();
  expect(screen.getByText('Resume').closest('button')?.hasAttribute('disabled')).toBe(false);
});

function chainSnapshot(spot = 500, iv = 0.2, expiryMs = 1_790_000_000_000): IbkrChainSnapshot {
  return {
    symbol: 'SPY', expiry_ms: expiryMs, underlying_price: spot, as_of_ms: 1_789_000_000_000,
    quotes: [{
      symbol: 'SPY', expiry_ms: expiryMs, strike: 500, right: 'C', bid: 5, ask: 5.2,
      last: null, bid_size: null, ask_size: null, iv, delta: 0.4, gamma: 0.01,
      theta: null, vega: null, underlying_price: spot, greeks_source: 'model', ts_ms: 1_789_000_000_000,
    }],
  };
}

async function renderWithEngine(priceOption: ReturnType<typeof vi.fn>) {
  const { fixture } = await render(BrokerOptionsChainComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerHealthService, useValue: { health: signal(null), bannerState: signal(null), start: vi.fn() } },
      { provide: MarketDataFeedService, useValue: { expirations: vi.fn(), strikes: vi.fn() } },
      { provide: QuantLibService, useValue: { priceOption } },
    ],
  });
  const component = fixture.componentInstance;
  const offer = (snap: IbkrChainSnapshot) => {
    component['lastSnapshot'].set(snap);
    component['maybeRepriceEngine'](snap);
  };
  return { component, offer };
}

it('reprices when IV, spot or expiry moves despite an unchanged bid/ask mid', async () => {
  const priceOption = vi.fn()
    .mockResolvedValueOnce({ success: true, delta: 0.5, gamma: 0.01 })
    .mockResolvedValueOnce({ success: true, delta: 0.6, gamma: 0.02 })
    .mockResolvedValueOnce({ success: true, delta: 0.7, gamma: 0.03 })
    .mockResolvedValueOnce({ success: true, delta: 0.8, gamma: 0.04 });
  const { component, offer } = await renderWithEngine(priceOption);

  offer(chainSnapshot());
  await waitFor(() => expect(component.rows()[0].callEngineDelta).toBe(0.5));
  offer(chainSnapshot());
  expect(priceOption).toHaveBeenCalledTimes(1);

  offer(chainSnapshot(500, 0.25));
  await waitFor(() => expect(component.rows()[0].callEngineDelta).toBe(0.6));
  offer(chainSnapshot(501, 0.25));
  await waitFor(() => expect(component.rows()[0].callEngineDelta).toBe(0.7));
  offer(chainSnapshot(501, 0.25, 1_790_086_400_000));
  await waitFor(() => expect(component.rows()[0].callEngineDelta).toBe(0.8));
  expect(priceOption).toHaveBeenCalledTimes(4);
});

it('clears old Greeks after a failed current calculation and retries that input', async () => {
  const priceOption = vi.fn()
    .mockResolvedValueOnce({ success: true, delta: 0.5, gamma: 0.01 })
    .mockResolvedValueOnce({ success: false, error: 'No engine result' })
    .mockResolvedValueOnce({ success: true, delta: 0.6, gamma: 0.02 })
    .mockRejectedValueOnce(new Error('Engine unavailable'));
  const { component, offer } = await renderWithEngine(priceOption);

  offer(chainSnapshot());
  await waitFor(() => expect(component.rows()[0].callEngineDelta).toBe(0.5));
  offer(chainSnapshot(500, 0.25));
  await waitFor(() => expect(component.engineError()).toBeInstanceOf(Error));
  expect(component.rows()[0].callEngineDelta).toBeNull();

  offer(chainSnapshot(500, 0.25));
  await waitFor(() => expect(component.rows()[0].callEngineDelta).toBe(0.6));
  expect(component.engineError()).toBeNull();
  offer(chainSnapshot(501, 0.25));
  await waitFor(() => expect(component.engineError()).toBeInstanceOf(Error));
  expect(component.rows()[0].callEngineDelta).toBeNull();
  expect(priceOption).toHaveBeenCalledTimes(4);
});

it('does not let an older IV result overwrite the current comparison at the same mid', async () => {
  let answerOld: ((result: { success: boolean; delta: number; gamma: number }) => void) | undefined;
  const priceOption = vi.fn()
    .mockImplementationOnce(() => new Promise<{ success: boolean; delta: number; gamma: number }>((resolve) => { answerOld = resolve; }))
    .mockResolvedValueOnce({ success: true, delta: 0.7, gamma: 0.03 });
  const { component, offer } = await renderWithEngine(priceOption);

  offer(chainSnapshot());
  offer(chainSnapshot(500, 0.25));
  await waitFor(() => expect(component.rows()[0].callEngineDelta).toBe(0.7));
  answerOld?.({ success: true, delta: 0.5, gamma: 0.01 });
  await Promise.resolve();
  expect(component.rows()[0].callEngineDelta).toBe(0.7);
});
