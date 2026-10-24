import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter, withComponentInputBinding, withRouterConfig } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../../environments/environment';
import type { IbkrConnectionHealth } from '../../api/broker-models';
import { DataLabRangePresetsService } from '../../services/data-lab-range-presets.service';
import { BrokerHealthService } from '../../services/broker-health.service';
import { MarketDataFeedService } from '../../services/market-data-feed.service';
import { fakePickerWorld, pickSymbol } from '../../shared/symbol-picker/testing/fake-picker-world';
import { MARKETS_ROUTES } from '../markets.routes';

// The pricing canvas is not exercised here: which view stands under which instrument is.
vi.mock('lightweight-charts', () => {
  const series = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
  const chart = () => ({
    addSeries: vi.fn(series),
    removeSeries: vi.fn(),
    remove: vi.fn(),
    applyOptions: vi.fn(),
    timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
    subscribeCrosshairMove: vi.fn(),
  });
  return {
    createChart: vi.fn(chart),
    LineSeries: 'LineSeries',
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
    CrosshairMode: { Normal: 0, Magnet: 1 },
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

const EXPIRATIONS_URL = `${environment.pythonServiceUrl}/api/options/expirations`;
const WINDOW_QUERY = 'from=2026-04-15&to=2026-05-15';
const EXPIRY = 1_790_000_000_000;

const CONNECTED: IbkrConnectionHealth = {
  connected: true, client_id: 1, connection_state: 'connected', fetched_at_ms: 0,
  host: 'localhost', last_transition_ms: 0, mode: 'paper', port: 4002,
};

function world() {
  return fakePickerWorld(
    [
      { symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
      { symbol: 'QQQ', name: 'Invesco QQQ', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
    ],
    [
      { symbol: 'SPY', name: 'SPY Inc.', exchange: 'XNAS', asset_class: 'us_equity', status: 'active' },
      { symbol: 'QQQ', name: 'QQQ Inc.', exchange: 'XNAS', asset_class: 'us_equity', status: 'active' },
    ],
  );
}

/** Opens an address through the Markets routes, with the IBKR feed standing by and every other read faked. */
async function open(url: string) {
  const pickers = world();
  const expirations = vi.fn().mockResolvedValue({ expirations_ms: [] });
  TestBed.configureTestingModule({
    providers: [
      provideRouter(
        [{ path: 'markets', loadChildren: () => MARKETS_ROUTES }],
        withComponentInputBinding(),
        withRouterConfig({ paramsInheritanceStrategy: 'always' }),
      ),
      provideHttpClient(),
      provideHttpClientTesting(),
      ...pickers.providers,
      { provide: DataLabRangePresetsService, useValue: { presets: vi.fn(async () => []) } },
      { provide: BrokerHealthService, useValue: { health: signal(CONNECTED), bannerState: signal(null), start: vi.fn() } },
      { provide: MarketDataFeedService, useValue: { expirations } },
    ],
  });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  harness.fixture.detectChanges();
  return { harness, expirations, router: TestBed.inject(Router), http: TestBed.inject(HttpTestingController) };
}

/** The expirations the Pricing view asked for, by underlying. */
function pricingExpirationRequests(http: HttpTestingController) {
  return http.match(EXPIRATIONS_URL).map((request) => request.request.body['underlying_ticker'] as string);
}

describe('the Options tab of an instrument\'s place', () => {
  it('opens the Surface view under the instrument, and names the tab and the view it is on', async () => {
    const { router, expirations } = await open(`/markets/SPY/options/surface?${WINDOW_QUERY}`);

    expect(router.url).toBe(`/markets/SPY/options/surface?${WINDOW_QUERY}`);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('SPY');
    expect(screen.getByRole('link', { name: 'Options' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: 'Surface' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: 'Pricing' }).getAttribute('aria-current')).toBeNull();
    // The view is the live surface for the instrument the place is open on, with its own inputs.
    await waitFor(() => expect(expirations).toHaveBeenCalledWith('SPY'));
    expect(screen.getByRole('spinbutton', { name: 'Days out' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Load Surface' })).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: 'Symbol' }), 'the instrument is the symbol; the view asks for none').toBeNull();
  });

  it('moves to the Pricing view under the same instrument, which reads that instrument\'s expirations and leaves Surface\'s inputs behind', async () => {
    const { router, http } = await open(`/markets/SPY/options/surface?${WINDOW_QUERY}&expiry=${EXPIRY}`);

    await userEvent.click(screen.getByRole('link', { name: 'Pricing' }));

    await waitFor(() => expect(router.url).toBe(`/markets/SPY/options/pricing?${WINDOW_QUERY}&expiry=${EXPIRY}`));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('SPY');
    await waitFor(() => expect(screen.getByRole('link', { name: 'Pricing' }).getAttribute('aria-current')).toBe('page'));
    expect(screen.getByRole('button', { name: /Reload/ })).toBeTruthy();
    expect(screen.getByText('Quick Start'), 'the Pricing view\'s own guide').toBeTruthy();
    expect(screen.queryByRole('spinbutton', { name: 'Days out' }), 'Surface\'s inputs stay in Surface').toBeNull();
    await waitFor(() => expect(pricingExpirationRequests(http)).toEqual(['SPY']));
  });

  it('keeps the context and the expiry in the address through the strip', async () => {
    await open(`/markets/SPY/options/surface?${WINDOW_QUERY}&expiry=${EXPIRY}`);

    expect(screen.getByRole('link', { name: 'Pricing' }).getAttribute('href')).toBe(
      `/markets/SPY/options/pricing?from=2026-04-15&to=2026-05-15&expiry=${EXPIRY}`,
    );
    expect(screen.getByRole('link', { name: 'Surface' }).getAttribute('href')).toBe(
      `/markets/SPY/options/surface?from=2026-04-15&to=2026-05-15&expiry=${EXPIRY}`,
    );
  });

  it('stays on the view it is on when another instrument is picked, and the view follows the new instrument', async () => {
    const { harness, router, http } = await open(`/markets/SPY/options/pricing?${WINDOW_QUERY}`);
    await waitFor(() => expect(pricingExpirationRequests(http)).toEqual(['SPY']));

    pickSymbol(harness.fixture, 'QQQ');

    await waitFor(() => expect(router.url).toBe(`/markets/QQQ/options/pricing?${WINDOW_QUERY}`));
    await waitFor(() => expect(pricingExpirationRequests(http)).toEqual(['QQQ']));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('QQQ');
  });

  it('asks IBKR for the new instrument\'s expiries when Surface is on and another instrument is picked', async () => {
    const { harness, router, expirations } = await open(`/markets/SPY/options/surface?${WINDOW_QUERY}`);
    await waitFor(() => expect(expirations).toHaveBeenCalledWith('SPY'));

    pickSymbol(harness.fixture, 'QQQ');
    await waitFor(() => expect(router.url).toBe(`/markets/QQQ/options/surface?${WINDOW_QUERY}`));
    await userEvent.click(screen.getByRole('button', { name: 'Setup' }));

    await waitFor(() => expect(expirations).toHaveBeenCalledWith('QQQ'));
  });
});
