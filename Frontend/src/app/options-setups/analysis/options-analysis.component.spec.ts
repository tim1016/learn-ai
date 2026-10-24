import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { components } from '../../api/broker.types';
import { environment } from '../../../environments/environment';
import { etIsoDate } from '../../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../../shared/option-contract/option-expiry-date';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { OPTIONS_SETUPS_ROUTES } from '../options-setups.routes';
import { ANALYSIS_NOT_A_SETUP_REASON } from './options-analysis.component';

vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn(), createPriceLine: vi.fn() });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    CandlestickSeries: 'CandlestickSeries',
    BaselineSeries: 'BaselineSeries',
    LineSeries: 'LineSeries',
    HistogramSeries: 'HistogramSeries',
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
    CrosshairMode: { Normal: 0, Magnet: 1 },
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

type Schema = components['schemas'];

const API = `${environment.pythonServiceUrl}/api`;

/** Everything the analysis may ask the server for: market data and its own payoff arithmetic, never a saved setup. */
const ANALYSIS_ROUTES = new Set(['options/expirations', 'snapshot/ticker', 'snapshot/options-chain', 'strategy/analyze']);

async function openAnalysis() {
  TestBed.configureTestingModule({
    providers: [
      provideRouter([{ path: 'options-setups', children: OPTIONS_SETUPS_ROUTES }]),
      provideHttpClient(),
      provideHttpClientTesting(),
      // The ticker picker must not construct the real catalog/jobs chain.
      ...fakePickerWorld().providers,
    ],
  });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl('/options-setups/analysis');
  harness.detectChanges();
  return { harness, http: TestBed.inject(HttpTestingController), router: TestBed.inject(Router) };
}

function chainOf(expiry: number): Schema['OptionsChainSnapshotResponse'] {
  const contract = (type: 'call' | 'put') => ({
    ticker: `O:SPY${type === 'call' ? 'C' : 'P'}100`, contract_type: type, strike_price: 100, expiration_ms: expiry,
    break_even_price: null, implied_volatility: 0.2, open_interest: 10, greeks: null, day: null,
    last_trade: null, last_quote: { bid: 4.9, ask: 5.1, bid_size: 1, ask_size: 1, midpoint: 5, timeframe: null },
  });
  return {
    success: true,
    underlying: { ticker: 'SPY', price: 100, change: 0, change_percent: 0 },
    contracts: [contract('call'), contract('put')],
    count: 2,
    risk_free_rate: 0.04,
    dividend_yield: null,
    rate_source: 'FRED',
    dividend_source: null,
    error: null,
  };
}

describe('the analytical Strategy Builder under Options setups (T16)', () => {
  beforeEach(() => TestBed.resetTestingModule());

  afterEach(() => {
    // The builder asks for a fresh analysis after each leg edit; none of these specs read it.
    TestBed.inject(HttpTestingController).match(() => true).forEach((request) => {
      if (!request.cancelled) request.flush({});
    });
  });

  it('opens at its own address, labelled as an analysis that is not a deployable setup, and says why it cannot become one', async () => {
    const { harness } = await openAnalysis();

    const page = within(harness.routeNativeElement as HTMLElement);
    expect(page.getByRole('heading', { level: 1, name: 'Strategy Builder' })).toBeTruthy();
    expect(page.getByText('Analytical: not a deployable setup')).toBeTruthy();
    expect(page.getByText(ANALYSIS_NOT_A_SETUP_REASON)).toBeTruthy();
    expect(page.getByRole('link', { name: 'Strategy Builder' }).getAttribute('aria-current')).toBe('page');
    expect(page.getByRole('link', { name: 'Strategy Finder (not available)' }).getAttribute('href')).toBe('/options-setups/analysis/finder');
  });

  it('still builds legs from a chain with its own Long and Short buttons', async () => {
    const { harness, http } = await openAnalysis();
    const page = within(harness.routeNativeElement as HTMLElement);
    const expiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86_400_000));

    await userEvent.click(page.getByRole('button', { name: 'Load' }));
    http.expectOne(`${API}/options/expirations`).flush({ success: true, expirations: [expiry], count: 1, error: null });
    http.match(`${API}/snapshot/ticker`).forEach((request) => request.flush({ success: false, snapshot: null, error: null }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    http.expectOne(`${API}/snapshot/options-chain`).flush(chainOf(expiry));
    await harness.fixture.whenStable();
    harness.detectChanges();

    expect(page.getByText('Legs (0)')).toBeTruthy();
    await userEvent.click(within(page.getByRole('group', { name: 'Call actions' })).getByRole('button', { name: 'S' }));
    await userEvent.click(within(page.getByRole('group', { name: 'Put actions' })).getByRole('button', { name: 'L' }));
    harness.detectChanges();

    expect(page.getByText('Legs (2)')).toBeTruthy();
    expect(page.getByText('SELL')).toBeTruthy();
    expect(page.getByText('BUY')).toBeTruthy();
  });

  it('offers no way to turn a fixed-leg analysis into a saved setup: the one control that would is disabled, and nothing leaves the analysis routes', async () => {
    const { harness, http, router } = await openAnalysis();
    const page = within(harness.routeNativeElement as HTMLElement);
    const expiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86_400_000));
    const asked: string[] = [];
    const note = (url: string) => asked.push(url.replace(`${API}/`, ''));

    await userEvent.click(page.getByRole('button', { name: 'Load' }));
    http.expectOne(`${API}/options/expirations`).flush({ success: true, expirations: [expiry], count: 1, error: null });
    http.match(`${API}/snapshot/ticker`).forEach((request) => request.flush({ success: false, snapshot: null, error: null }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    http.expectOne(`${API}/snapshot/options-chain`).flush(chainOf(expiry));
    await harness.fixture.whenStable();
    harness.detectChanges();
    await userEvent.click(within(page.getByRole('group', { name: 'Call actions' })).getByRole('button', { name: 'L' }));
    harness.detectChanges();

    const useAsSetup = page.getByRole('button', { name: 'Use as setup' }) as HTMLButtonElement;
    await userEvent.click(useAsSetup);
    harness.detectChanges();

    expect(useAsSetup.disabled).toBe(true);
    expect(router.url).toBe('/options-setups/analysis');
    expect(page.getByText(ANALYSIS_NOT_A_SETUP_REASON)).toBeTruthy();
    const setupLinks = page.queryAllByRole('link').filter((link) => /^\/options-setups\/[^/]+\/(overview|define|explore|golden)/.test(link.getAttribute('href') ?? ''));
    expect(setupLinks.map((link) => link.getAttribute('href'))).toEqual([]);
    expect(page.queryAllByRole('button', { name: /save|backtest|deploy|approve/i })).toEqual([]);
    http.match(() => true).forEach((request) => {
      note(request.request.url);
      if (!request.cancelled) request.flush({});
    });
    expect(asked.filter((route) => !ANALYSIS_ROUTES.has(route))).toEqual([]);
  });
});
