import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { fakeScopedHealth, healthItem, projection } from '../../../shared/scoped-health/testing/scoped-health-fixtures';
import { MarketDataFeedService } from '../../../services/market-data-feed.service';
import type { IbkrConnectionHealth } from '../../../api/broker-models';
import { InstrumentContextService } from '../../instrument-context.service';
import { OptionsSurfaceComponent, intersectAll, pickMonthlyExpiries, pickStrikesAroundAtm } from './options-surface.component';

/** The place the view sits in, reduced to the one thing it reads: the instrument. */
function instrumentOf(symbol: string) {
  const current = signal(symbol);
  return { current, provider: { provide: InstrumentContextService, useValue: { symbol: current } } };
}

describe('Options Surface view', () => {
  it('starts the health poll, opens its controls when connected, and does not retry a failed expiry read forever', async () => {
    const health = signal<IbkrConnectionHealth | null>(null);
    const start = vi.fn();
    const expirations = vi.fn().mockRejectedValue(new Error('feed unavailable'));
    const { fixture } = await render(OptionsSurfaceComponent, {
      providers: [
        provideRouter([]),
        instrumentOf('SPY').provider,
        { provide: BrokerHealthService, useValue: { health, bannerState: signal(null), start } },
        { provide: MarketDataFeedService, useValue: { expirations } },
      ],
    });

    expect(start).toHaveBeenCalledOnce();
    expect(screen.getByText(/Broker is disconnected/)).toBeTruthy();
    health.set({ connected: true, client_id: 1, connection_state: 'connected', fetched_at_ms: 0,
      host: 'localhost', last_transition_ms: 0, mode: 'paper', port: 4002 });
    fixture.detectChanges();
    await waitFor(() => expect(screen.getByText('Setup')).toBeTruthy());
    await waitFor(() => expect(screen.getByText('Surface setup failed.')).toBeTruthy());
    expect(expirations).toHaveBeenCalledOnce();
    expect(expirations).toHaveBeenCalledWith('SPY');
  });

  it('says what the data plane makes of the IBKR feed, and keeps its own reason where the controls would be', async () => {
    const feed = fakeScopedHealth(
      projection(healthItem({ kind: 'ibkr_market_data', severity: 'critical', explanation: 'The IBKR session is down. Live option chain and bot feed readiness are affected; archived history is not.' })),
    );
    await render(OptionsSurfaceComponent, {
      providers: [
        provideRouter([]),
        instrumentOf('SPY').provider,
        { provide: BrokerHealthService, useValue: { health: signal(null), bannerState: signal(null), start: vi.fn() } },
        { provide: MarketDataFeedService, useValue: { expirations: vi.fn() } },
        feed.provider,
      ],
    });

    expect((await screen.findByText(/archived history is not/)).closest('.health-notice')?.textContent).toContain('Failing.');
    expect(feed.read).toHaveBeenCalledWith({ operations: ['live_chain'], instrument: null });
    expect(screen.getByText(/Broker is disconnected/)).toBeTruthy();
  });

  it('clears the prepared grid when the instrument changes', async () => {
    const instrument = instrumentOf('SPY');
    const { fixture } = await render(OptionsSurfaceComponent, {
      providers: [
        provideRouter([]),
        instrument.provider,
        { provide: BrokerHealthService, useValue: { health: signal<IbkrConnectionHealth>({ connected: true,
          client_id: 1, connection_state: 'connected', fetched_at_ms: 0, host: 'localhost',
          last_transition_ms: 0, mode: 'paper', port: 4002 }), bannerState: signal(null), start: vi.fn() } },
        { provide: MarketDataFeedService, useValue: { expirations: vi.fn().mockResolvedValue({ expirations_ms: [] }) } },
      ],
    });
    const page = fixture.componentInstance;
    page.readyExpiries.set([Date.now() + 86_400_000]);
    page.readyStrikes.set([500]);
    fixture.detectChanges();
    expect(screen.getByText('Load Surface').closest('button')?.disabled).toBe(false);

    instrument.current.set('QQQ');
    fixture.detectChanges();
    expect(screen.getByText('Load Surface').closest('button')?.disabled).toBe(true);
    expect(page.allExpirations()).toEqual([]);

    page.paused.set(true);
    fixture.detectChanges();
    expect(screen.getByText('Resume')).toBeTruthy();
  });
});

describe('pickMonthlyExpiries', () => {
  it('selects the third Friday amid daily expiries, including the Thursday holiday listing', () => {
    const expiry = (day: number) => Date.UTC(2026, 9, day, 20);
    expect(pickMonthlyExpiries([13, 14, 15, 16, 19, 20].map(expiry))).toEqual([expiry(16)]);
    expect(pickMonthlyExpiries([13, 14, 15, 19, 20].map(expiry))).toEqual([expiry(15)]);
  });
});

describe('pickStrikesAroundAtm', () => {
  it.each([
    ['returns ±band strikes around the closest-to-ATM strike', [400, 405, 410, 415, 420, 425, 430, 435, 440], 420, 2, [410, 415, 420, 425, 430]],
    ['clips at the low edge of the grid', [400, 405, 410, 415, 420], 401, 3, [400, 405, 410, 415]],
    ['clips at the high edge of the grid', [400, 405, 410, 415, 420], 419, 3, [405, 410, 415, 420]],
    // 407.5 is equidistant from 405 and 410; first-found wins → 405.
    ['handles ATM exactly between two strikes by snapping to the nearer of the two (ties = lower)', [400, 405, 410, 415, 420], 407.5, 1, [400, 405, 410]],
    ['returns an empty list when given no strikes', [], 420, 5, []],
  ])('%s', (_name, grid, atm, band, expected) => {
    expect(pickStrikesAroundAtm(grid, atm, band)).toEqual(expected);
  });
});

describe('intersectAll', () => {
  it.each([
    ['the strikes present in every list', [
      [400, 405, 410, 415],
      [405, 410, 415, 420],
      [400, 410, 415],
    ], [410, 415]],
    ['the single list unchanged when given one input', [[400, 405, 410]], [400, 405, 410]],
  ])('returns %s', (_name, lists, expected) => {
    const result = intersectAll(lists).sort((a, b) => a - b);
    expect(result).toEqual(expected);
  });

  it.each([
    ['no common strike exists', [
      [400, 405],
      [410, 415],
    ]],
    ['given no inputs', []],
  ])('returns an empty list when %s', (_name, lists) => {
    expect(intersectAll(lists)).toEqual([]);
  });
});
