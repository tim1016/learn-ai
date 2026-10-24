import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { Router, provideRouter, withComponentInputBinding, withRouterConfig } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { from, of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BrokerHealthService } from '../../services/broker-health.service';
import { MarketDataFeedService } from '../../services/market-data-feed.service';
import { QuantLibService } from '../../services/quantlib.service';
import { OptionsBacktestService } from '../../components/options-backtest/options-backtest.service';
import { deferred } from '../../components/options-backtest/testing/deferred';
import { SERVED_LIMITS } from '../../components/options-backtest/testing/fixtures';
import { reads } from '../../components/options-backtest/testing/page-actions';
import { MarketDataService } from '../../services/market-data.service';
import type { OptionsChainSnapshotResult, SnapshotContractResult } from '../../services/market-data.types';
import { DataLabRangePresetsService, type ChartRangePreset } from '../../services/data-lab-range-presets.service';
import { optionExpiryAnchorMs } from '../../shared/option-contract/option-expiry-date';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { RunSessionService } from '../../services/run-session.service';
import { ScopedHealthService } from '../../shared/scoped-health/scoped-health.service';
import { InstrumentFactsService } from '../facts/instrument-facts.service';
import { QUIET_FACTS, QUIET_HEALTH, idleRunSession } from '../testing/instrument-place';
import { InstrumentContextService } from '../instrument-context.service';
import { MARKETS_ROUTES } from '../markets.routes';
import { InstrumentWorkspaceComponent } from '../workspace/instrument-workspace.component';
import { PAST_LIVE_REASON } from './chain/chain-modes';
import { PastChainService } from './chain/archive/past-chain.service';
import { disconnectedHealth, ibkrFrame } from './chain/live/testing/fake-ibkr';
import { liveWorld, startWith, type LiveWorld } from './chain/live/testing/render-live';
import { chainDay, chainExpiry, contract, EXPIRY_MS, SESSION_CLOSE_MS, side, TRADING_DATE } from './chain/archive/testing/fixtures';

const NEAR = optionExpiryAnchorMs('2099-01-15');
const FAR = optionExpiryAnchorMs('2099-02-19');
const WINDOW = 'from=2026-04-15&to=2026-05-15';

const PRESET_1Y: ChartRangePreset = {
  key: '1Y',
  label: 'Past year',
  start_ms_utc: Date.UTC(2025, 8, 22),
  end_ms_utc: Date.UTC(2026, 8, 18) + 86_400_000 - 1,
  session_count: 251,
  estimated_bars_per_timeframe: { '1D': 251 },
};

function snapshotContract(contractType: 'call' | 'put', strike: number): SnapshotContractResult {
  return {
    ticker: `O:SPY${strike}${contractType[0].toUpperCase()}`,
    contractType,
    strikePrice: strike,
    expirationMs: NEAR,
    breakEvenPrice: null,
    impliedVolatility: 0.2,
    openInterest: 100,
    greeks: { delta: 0.5, gamma: 0.01, theta: -0.05, vega: 0.1 },
    day: { open: 1, high: 1, low: 1, close: 1, volume: 1000, vwap: 1 },
    lastTrade: null,
    lastQuote: null,
  };
}

function snapshotReply(strike = 700): OptionsChainSnapshotResult {
  const contracts = [snapshotContract('call', strike), snapshotContract('put', strike)];
  return {
    success: true,
    underlying: { ticker: 'SPY', price: strike, change: 0, changePercent: 0 },
    contracts,
    count: 2,
    riskFreeRate: null,
    dividendYield: null,
    rateSource: null,
    dividendSource: null,
    error: null,
  };
}

/** The Options tab, with every read it can make replaced: the provider snapshot, the archive and the limits. */
async function open(url: string, market = { chain: vi.fn(() => of(snapshotReply())) }, archive = { day: vi.fn(async () => chainDay()), expiry: vi.fn(async () => chainExpiry()), download: vi.fn() }, live: LiveWorld = liveWorld(vi)) {
  const pickers = fakePickerWorld([{ symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' }]);
  TestBed.configureTestingModule({
    providers: [
      provideRouter([{ path: 'markets', loadChildren: () => MARKETS_ROUTES }], withComponentInputBinding(), withRouterConfig({ paramsInheritanceStrategy: 'always' })),
      provideHttpClient(),
      provideHttpClientTesting(),
      ...pickers.providers,
      { provide: DataLabRangePresetsService, useValue: { presets: vi.fn(async () => [PRESET_1Y]) } },
      // The place's header, dock and lake notice read the data plane; this spec is about the Options tab, so each is told nothing.
      { provide: RunSessionService, useValue: idleRunSession() },
      { provide: ScopedHealthService, useValue: QUIET_HEALTH },
      { provide: InstrumentFactsService, useValue: QUIET_FACTS },
      {
        provide: MarketDataService,
        useValue: {
          getOptionsExpirations: vi.fn(() => of([NEAR, FAR])),
          getStockSnapshot: vi.fn(() => of({ success: false, snapshot: null, error: null })),
          getOptionsChainSnapshot: market.chain,
        },
      },
      { provide: PastChainService, useValue: archive },
      { provide: OptionsBacktestService, useValue: { limits: async () => SERVED_LIMITS } },
      // The retained IBKR feed is faked at every seam: no spec connects to a Gateway.
      { provide: BrokerHealthService, useValue: { health: live.health, lastError: live.healthFailure, start: vi.fn() } },
      { provide: MarketDataFeedService, useValue: { expirations: live.expirations, strikes: live.strikes } },
      { provide: QuantLibService, useValue: { selectedEngine: live.engine, priceOption: live.price } },
      live.feed.provide,
    ],
  });
  const harness = await RouterTestingHarness.create();
  const router = TestBed.inject(Router);
  await harness.navigateByUrl(url);
  harness.fixture.detectChanges();
  await harness.fixture.whenStable();
  const workspace = () => harness.fixture.debugElement.query(By.directive(InstrumentWorkspaceComponent));
  return { harness, router, market, archive, live, context: () => workspace().injector.get(InstrumentContextService) };
}

const modeButton = (name: string) => screen.getByRole('button', { name });
const query = (router: Router) => router.parseUrl(router.url).queryParams;
const facts = () => reads(document.querySelector('app-chain-facts'));

describe('the Options tab of an instrument', () => {
  afterEach(() => vi.useRealTimers());

  it('is routed beside Chart: Options is open, the Chain view is open, and Surface and Pricing are views beside it that keep the expiry the chain opened on', async () => {
    await open('/markets/SPY/options/chain');

    expect(screen.getByRole('link', { name: 'Options' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: 'Chain' }).getAttribute('aria-current')).toBe('page');
    await waitFor(() => expect(screen.getByRole('link', { name: 'Surface' }).getAttribute('href')).toBe(`/markets/SPY/options/surface?expiry=${NEAR}`));
    expect(screen.getByRole('link', { name: 'Pricing' }).getAttribute('href')).toBe(`/markets/SPY/options/pricing?expiry=${NEAR}`);
  });

  it('explains itself with the manual chapter on the chain, not the chart\'s reference', async () => {
    await open('/markets/SPY/options/chain');

    expect(screen.getByRole('link', { name: /How this works/ }).getAttribute('href')).toBe('/docs/options-backtest-manual#past-chain');
  });

  it('opens on the Chain view, keeping the query it was given', async () => {
    const { router } = await open(`/markets/SPY/options?${WINDOW}&expiry=${NEAR}`);

    expect(router.url).toBe(`/markets/SPY/options/chain?${WINDOW}&expiry=${NEAR}`);
  });

  it('opens Now · Snapshot by default, says its source and as-of, and writes the expiry it opened on into the address', async () => {
    const { router, market } = await open('/markets/SPY/options/chain');

    await screen.findByRole('table', { name: /Calls on the left/ });
    await waitFor(() => expect(query(router)['expiry']).toBe(String(NEAR)));

    expect(modeButton('Now · Snapshot').getAttribute('aria-pressed')).toBe('true');
    expect(facts()).toContain('Polygon option-chain snapshot');
    expect(facts()).toMatch(/Quotes received \d{1,2}:\d{2}/);
    expect(market.chain).toHaveBeenCalledWith('SPY', NEAR);
  });

  it('keeps the chart\'s window out of the Options tab: no window, timeframe or session control, and a window the chart cannot use stops nothing', async () => {
    await open('/markets/SPY/options/chain?from=2026-06-01&to=2026-05-01');

    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(screen.queryByText('Window')).toBeNull();
    expect(screen.queryByText('Timeframe')).toBeNull();
    expect(screen.queryByText(/cannot be used, so nothing is loaded/)).toBeNull();
  });
});

describe('the modes of the chain', () => {
  afterEach(() => vi.useRealTimers());

  it('disables Past · Live IBKR with its reason in words, and leaves Now · Live IBKR open to choose', async () => {
    const { router } = await open('/markets/SPY/options/chain');
    await screen.findByRole('table', { name: /Calls on the left/ });

    expect((modeButton('Past · Live IBKR') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(PAST_LIVE_REASON)).toBeTruthy();
    expect(modeButton('Past · Live IBKR').getAttribute('title')).toBe(PAST_LIVE_REASON);
    expect((modeButton('Now · Live IBKR') as HTMLButtonElement).disabled).toBe(false);
    await userEvent.setup().click(modeButton('Past · Live IBKR'));
    expect(query(router)['source']).toBeUndefined();
  });

  it('switches to Past · Archive: the address keeps the expiry and asks for a session date, the snapshot is gone, and nothing is read', async () => {
    const { router, archive } = await open(`/markets/SPY/options/chain?expiry=${NEAR}`);
    await screen.findByRole('table', { name: /Calls on the left/ });

    await userEvent.setup().click(modeButton('Past · Archive'));

    expect(await screen.findByText('No session date is picked.')).toBeTruthy();
    expect(query(router)).toMatchObject({ source: 'archive', expiry: String(NEAR) });
    expect(modeButton('Past · Archive').getAttribute('aria-pressed')).toBe('true');
    expect(screen.queryByText(/Polygon option-chain snapshot/)).toBeNull();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(archive.day).not.toHaveBeenCalled();
  });

  it('shows a past session\'s archive with its own source and as-of, and the session date and instant are separate controls', async () => {
    const { router } = await open(`/markets/SPY/options/chain?source=archive&date=${TRADING_DATE}&expiry=${EXPIRY_MS['2026-10-16']}`);

    await screen.findByRole('table', { name: /Calls on the left/ });

    expect(modeButton('Past · Archive').getAttribute('aria-pressed')).toBe('true');
    expect(facts()).toContain('Past · Archive');
    expect(facts()).toContain('Session 2026-10-09');
    expect(facts()).toContain('Priced as of 16:00 ET');
    expect(query(router)['at']).toBeUndefined();
  });

  it('never shows a value observed after the instant the address asks at', async () => {
    const late = contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '9.99', timeMs: SESSION_CLOSE_MS + 60_000 });
    const archive = { day: vi.fn(async () => chainDay()), expiry: vi.fn(async () => chainExpiry({ calls: side('call', [late]), puts: side('put', []) })), download: vi.fn() };

    await open(`/markets/SPY/options/chain?source=archive&date=${TRADING_DATE}&expiry=${EXPIRY_MS['2026-10-16']}`, undefined, archive);

    const table = await screen.findByRole('table', { name: /Calls on the left/ });
    expect(table.textContent).toContain('Observed after this instant, so not shown');
    expect(table.textContent).not.toContain('9.99');
  });

  it('says each absent row\'s reason: not stored, not traded by then, unreadable, and an expiry the date does not list', async () => {
    const { archive } = await open(`/markets/SPY/options/chain?source=archive&date=${TRADING_DATE}&expiry=${EXPIRY_MS['2026-10-16']}`);

    const table = await screen.findByRole('table', { name: /Calls on the left/ });

    const text = reads(table);
    expect(text).toContain('Did not trade by then');
    expect(text).toContain('Listed; prices not stored');
    expect(text).toContain('Prices cannot be read');
    expect(text).toMatch(/\$2\.10 in the minute ending 15:59 ET/);
    archive.expiry.mockResolvedValue(chainExpiry({ expiry_listed: false, tag: null }));
    fireEvent.change(screen.getByLabelText('Trading date'), { target: { value: '2026-10-12' } });
    expect(await screen.findByText('The 2026-10-16 expiry is not listed on this date.')).toBeTruthy();
  });

  it('stops reading the snapshot when the mode is left, and an answer still in flight cannot reach the archive chain', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const slow = deferred<OptionsChainSnapshotResult>();
    const market = { chain: vi.fn(() => from(slow.promise)) };
    await open(`/markets/SPY/options/chain?expiry=${NEAR}`, market);
    await waitFor(() => expect(market.chain).toHaveBeenCalledTimes(1));

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(modeButton('Past · Archive'));
    await screen.findByText('No session date is picked.');
    slow.resolve(snapshotReply(111));
    await vi.advanceTimersByTimeAsync(20_000);

    expect(market.chain).toHaveBeenCalledTimes(1);
    expect(document.querySelector('app-snapshot-chain')).toBeNull();
    expect(document.body.textContent).not.toContain('111.00');
  });

  it('says Past · Live IBKR is not available when the address asks for a past date from the live source, and never shows today\'s chain for it', async () => {
    const { market, archive, live } = await open(`/markets/SPY/options/chain?source=live&date=${TRADING_DATE}`);

    expect(await screen.findByText('Past · Live IBKR is not available.')).toBeTruthy();
    expect(modeButton('Past · Live IBKR').getAttribute('aria-pressed')).toBe('true');
    expect(market.chain).not.toHaveBeenCalled();
    expect(archive.day).not.toHaveBeenCalled();
    expect(live.expirations).not.toHaveBeenCalled();
    expect(live.feed.streams).toHaveLength(0);
  });

  it('opens Now · Live IBKR from the strip with the expiry kept, the snapshot gone, and nothing subscribed', async () => {
    const { router, market, live } = await open(`/markets/SPY/options/chain?expiry=${NEAR}`);
    await screen.findByRole('table', { name: /Calls on the left/ });

    await userEvent.setup().click(modeButton('Now · Live IBKR'));

    expect(await screen.findByText('Nothing is subscribed.')).toBeTruthy();
    expect(query(router)).toMatchObject({ source: 'live', expiry: String(NEAR) });
    expect(modeButton('Now · Live IBKR').getAttribute('aria-pressed')).toBe('true');
    expect(facts()).toContain('Now · Live IBKR');
    expect(facts()).toContain('Data-plane paper session connected.');
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(document.querySelector('app-snapshot-chain')).toBeNull();
    expect(market.chain).toHaveBeenCalledTimes(1);
    expect(live.feed.streams).toHaveLength(0);
  });

  it('says Now · Live IBKR is unavailable when the data plane holds no Gateway session, and falls back to nothing', async () => {
    const live = liveWorld(vi);
    live.health.set(disconnectedHealth());
    const { market } = await open(`/markets/SPY/options/chain?source=live&expiry=${NEAR}`, undefined, undefined, live);

    expect(await screen.findByText('Now · Live IBKR is unavailable.')).toBeTruthy();
    expect(screen.getAllByText(/The FastAPI data-plane IBKR client is disconnected\./).length).toBeGreaterThan(0);
    expect(market.chain).not.toHaveBeenCalled();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(live.expirations).not.toHaveBeenCalled();
    expect(live.feed.streams).toHaveLength(0);
  });

  it('releases the live subscription when the mode changes, and a frame the old stream delivers late never reaches the next chain', async () => {
    const { live } = await open(`/markets/SPY/options/chain?source=live&expiry=${NEAR}`);
    await startWith(500);
    live.feed.streams[0].emit(ibkrFrame({ underlying_price: 111.11 }));
    await screen.findByRole('table', { name: /Calls on the left/ });
    expect(facts()).toContain('SPY 111.11');
    const user = userEvent.setup();

    await user.click(modeButton('Now · Snapshot'));

    await screen.findByText(/Polygon option-chain snapshot/);
    expect(live.feed.streams[0].closes).toBe(1);
    live.feed.streams[0].emit(ibkrFrame({ underlying_price: 222.22 }));
    expect(document.body.textContent).not.toContain('222.22');
    expect(document.body.textContent).not.toContain('Now · Live IBKR is unavailable');
    expect(document.querySelector('app-live-chain')).toBeNull();

    await user.click(modeButton('Past · Archive'));
    await screen.findByText('No session date is picked.');
    live.feed.streams[0].emit(ibkrFrame({ underlying_price: 333.33 }));
    expect(document.body.textContent).not.toContain('333.33');
    expect(live.feed.streams).toHaveLength(1);
  });

  it('says in place what an unusable address asks for, loads nothing, and is left by choosing a source', async () => {
    const { market, router } = await open(`/markets/SPY/options/chain?source=snapshot&date=${TRADING_DATE}`);

    expect(await screen.findByText('The chain asked for in this address cannot be read, so nothing is loaded.')).toBeTruthy();
    expect(screen.getByText(/Choose Past · Archive to read a past date/)).toBeTruthy();
    expect(market.chain).not.toHaveBeenCalled();

    await userEvent.setup().click(modeButton('Now · Snapshot'));

    await screen.findByRole('table', { name: /Calls on the left/ });
    expect(query(router)['date']).toBeUndefined();
  });
});

describe('the Options tab\'s own context and the chart\'s window', () => {
  it('moving the session date leaves the chart window as it was, and Chart still has it on return', async () => {
    const { router, context } = await open(`/markets/SPY/options/chain?${WINDOW}&source=archive&date=${TRADING_DATE}&expiry=${EXPIRY_MS['2026-10-16']}`);
    await screen.findByRole('table', { name: /Calls on the left/ });
    const chartWindow = context().context()?.window;
    expect(chartWindow).toEqual({ from: '2026-04-15', to: '2026-05-15' });

    fireEvent.change(screen.getByLabelText('Trading date'), { target: { value: '2026-10-12' } });

    await waitFor(() => expect(query(router)['date']).toBe('2026-10-12'));
    expect(query(router)).toMatchObject({ from: '2026-04-15', to: '2026-05-15' });
    expect(context().context()?.window).toEqual(chartWindow);
    const chart = screen.getByRole('link', { name: 'Chart' });
    expect(chart.getAttribute('href')).toBe('/markets/SPY/chart?from=2026-04-15&to=2026-05-15');
  });

  it('carries the expiry, and not the session date, to the other views of the tab', async () => {
    await open(`/markets/SPY/options/chain?${WINDOW}&source=archive&date=${TRADING_DATE}&expiry=${EXPIRY_MS['2026-10-16']}`);
    await screen.findByRole('table', { name: /Calls on the left/ });

    const chain = screen.getByRole('link', { name: 'Chain' });

    expect(chain.getAttribute('href')).toBe(`/markets/SPY/options/chain?${WINDOW}&expiry=${EXPIRY_MS['2026-10-16']}`);
  });
});
