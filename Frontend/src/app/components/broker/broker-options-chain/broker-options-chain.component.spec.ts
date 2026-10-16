import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { expect, it, vi } from 'vitest';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { MarketDataFeedService } from '../../../services/market-data-feed.service';
import { QuantLibService } from '../../../services/quantlib.service';
import type { IbkrConnectionHealth } from '../../../api/broker-models';
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
