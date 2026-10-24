import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { from, of, type Observable } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { deferred } from '../../../../components/options-backtest/testing/deferred';
import { reads } from '../../../../components/options-backtest/testing/page-actions';
import { MarketDataService } from '../../../../services/market-data.service';
import type { OptionsChainSnapshotResult, SnapshotContractResult, StockSnapshotResult, StockTickerSnapshot } from '../../../../services/market-data.types';
import { optionExpiryAnchorMs } from '../../../../shared/option-contract/option-expiry-date';
import { ADD_LEG_UNAVAILABLE_REASON } from '../chain-presentation';
import { SnapshotChainComponent } from './snapshot-chain.component';

const NEAR = optionExpiryAnchorMs('2099-01-15');
const FAR = optionExpiryAnchorMs('2099-02-19');

function contractAt(contractType: 'call' | 'put', strike: number, overrides: Partial<SnapshotContractResult> = {}): SnapshotContractResult {
  return {
    ticker: `O:SPY${strike}${contractType[0].toUpperCase()}`,
    contractType,
    strikePrice: strike,
    expirationMs: NEAR,
    standard: null,
    breakEvenPrice: null,
    impliedVolatility: 0.2,
    openInterest: 100,
    greeks: { delta: 0.5, gamma: 0.01, theta: -0.05, vega: 0.1 },
    day: { open: 1, high: 1, low: 1, close: 1, volume: 1000, vwap: 1 },
    lastTrade: null,
    lastQuote: null,
    ...overrides,
  };
}

function chainReply(strike: number, price = 700, contracts = [contractAt('call', strike), contractAt('put', strike)]): OptionsChainSnapshotResult {
  return {
    success: true,
    // The vendor fills the chain underlying's change from the first contract's break-even delta, not from the stock's move.
    underlying: { ticker: 'SPY', price, change: 9.87, changePercent: 9.87 },
    contracts,
    count: contracts.length,
    riskFreeRate: null,
    dividendYield: null,
    rateSource: null,
    dividendSource: null,
    error: null,
  };
}

function stock(overrides: Partial<StockTickerSnapshot> = {}): StockSnapshotResult {
  return {
    success: true,
    snapshot: { ticker: 'SPY', day: { open: 1, high: 1, low: 1, close: 700, volume: 1, vwap: 1 }, prevDay: null, min: null, todaysChange: 2.5, todaysChangePercent: 0.35, updated: null, ...overrides },
    error: null,
  };
}

interface Market {
  expirations: ReturnType<typeof vi.fn<(ticker: string) => Observable<number[]>>>;
  stock: ReturnType<typeof vi.fn<(ticker: string) => Observable<StockSnapshotResult>>>;
  chain: ReturnType<typeof vi.fn<(ticker: string, expiry?: number) => Observable<OptionsChainSnapshotResult>>>;
}

function market(changes: Partial<Market> = {}): Market {
  return {
    expirations: vi.fn(() => of([NEAR, FAR])),
    stock: vi.fn(() => of(stock())),
    chain: vi.fn(() => of(chainReply(700))),
    ...changes,
  };
}

async function renderChain(fake: Market, inputs: { symbol?: string; expiryMs?: number | null } = {}, on: { expiryPicked?: (ms: number) => void; expiryAdopted?: (ms: number) => void } = {}) {
  const view = await render(SnapshotChainComponent, {
    inputs: { symbol: 'SPY', expiryMs: null, ...inputs },
    on,
    providers: [
      {
        provide: MarketDataService,
        useValue: { getOptionsExpirations: fake.expirations, getStockSnapshot: fake.stock, getOptionsChainSnapshot: fake.chain },
      },
    ],
  });
  await view.fixture.whenStable();
  return view;
}

const facts = () => reads(document.querySelector('app-chain-facts'));

describe('Now · Snapshot, as a source of the chain', () => {
  afterEach(() => {
    vi.useRealTimers();
    localStorage.clear();
  });

  it('says which source it is, when the quotes arrived, whether they are delayed, and the stock\'s own move rather than the chain\'s break-even delta', async () => {
    await renderChain(market(), { expiryMs: NEAR });

    await screen.findByRole('table', { name: /Calls on the left/ });

    const text = facts();
    expect(text).toContain('Now · Snapshot');
    expect(text).toContain('Polygon option-chain snapshot, read through the data service.');
    expect(text).toMatch(/Received by this page \d{1,2}:\d{2}/);
    expect(text).toContain('not when the provider observed its quotes');
    expect(text).toContain('Quote state Not stated: the provider does not say whether these quotes are delayed.');
    expect(text).toContain('Underlying SPY 700.00, +2.50 (+0.35%)');
    expect(text).not.toContain('9.87');
  });

  it('draws a standard and an adjusted call at one strike on two rows, the adjusted one marked non-standard, with each symbol on hover', async () => {
    const standard = contractAt('call', 700, { ticker: 'O:SPY700C', standard: true });
    const adjusted = contractAt('call', 700, { ticker: 'O:SPY1700C', standard: false });
    await renderChain(market({ chain: vi.fn(() => of(chainReply(700, 700, [adjusted, standard, contractAt('put', 700, { standard: true })]))) }), { expiryMs: NEAR });

    await screen.findByRole('table', { name: /Calls on the left/ });

    const rows = [...document.querySelectorAll('tr.data-row')];
    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.querySelector('td.call-side.delta')?.getAttribute('title'))).toEqual(['O:SPY700C', 'O:SPY1700C']);
    expect(rows[0].textContent).not.toContain('Non-standard');
    expect(rows[1].textContent).toContain('Non-standard');
    expect(rows[1].textContent).toContain('No other contract at this strike');
  });

  it('shows a dash for the move when the stock snapshot is unavailable, and the chain still loads', async () => {
    const fake = market({ stock: vi.fn(() => of<StockSnapshotResult>({ success: false, snapshot: null, error: null })) });
    await renderChain(fake, { expiryMs: NEAR });

    await screen.findByRole('table', { name: /Calls on the left/ });

    expect(facts()).toContain('Underlying SPY 700.00, move —');
    expect(facts()).not.toContain('9.87');
  });

  it('says a delayed snapshot is delayed, as the provider labels it', async () => {
    const delayed = [contractAt('call', 700, { lastQuote: { bid: 1, ask: 2, bidSize: 1, askSize: 1, midpoint: 1.5, timeframe: 'DELAYED' } })];
    await renderChain(market({ chain: vi.fn(() => of(chainReply(700, 700, delayed))) }), { expiryMs: NEAR });

    await screen.findByRole('table', { name: /Calls on the left/ });

    expect(facts()).toContain('Quote state Delayed: the provider labels these quotes delayed.');
  });

  // 11:00 ET on Wed 2026-10-14. The expiry anchor is 16:00 ET on its date.
  it.each([
    ['2026-10-14', '0 DTE'],
    ['2026-10-15', '1 DTE'],
    ['2026-10-16', '2 DTE'],
  ])('counts calendar days to the expiry %s as %s', async (expiryDate, expected) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(Date.UTC(2026, 9, 14, 15, 0));
    const expiry = optionExpiryAnchorMs(expiryDate);

    await renderChain(market({ expirations: vi.fn(() => of([expiry])) }), { expiryMs: expiry });

    expect(await screen.findByText(expected)).toBeTruthy();
  });

  it('disables every L and S button, shows why, and a click starts nothing', async () => {
    const fake = market({ chain: vi.fn(() => of(chainReply(700, 700, [contractAt('call', 700), contractAt('put', 700)]))) });
    await renderChain(fake, { expiryMs: NEAR });

    const reason = await screen.findByText(ADD_LEG_UNAVAILABLE_REASON);
    const buttons = screen.getAllByRole('button', { name: /^Add (long|short) (call|put) at 700\.00$/ });
    expect(buttons).toHaveLength(4);
    for (const button of buttons) {
      expect(button).toHaveProperty('disabled', true);
      expect(button.getAttribute('title')).toBe(reason.textContent);
    }
    const reads = fake.chain.mock.calls.length;

    await userEvent.setup().click(buttons[0]);

    expect(fake.chain.mock.calls.length).toBe(reads);
  });
});

describe('the expiry of the snapshot', () => {
  it('opens the nearest the provider lists when the address names none, reads that chain, and hands it up to be written', async () => {
    const adopted = vi.fn();
    const fake = market();

    await renderChain(fake, {}, { expiryAdopted: adopted });

    await waitFor(() => expect(adopted).toHaveBeenCalledWith(NEAR));
    await waitFor(() => expect(fake.chain).toHaveBeenCalledWith('SPY', NEAR));
  });

  it('reads the expiry the address names, and adopts nothing', async () => {
    const adopted = vi.fn();
    const fake = market();

    await renderChain(fake, { expiryMs: FAR }, { expiryAdopted: adopted });

    await waitFor(() => expect(fake.chain).toHaveBeenCalledWith('SPY', FAR));
    expect(adopted).not.toHaveBeenCalled();
  });

  it('says an expiry the provider does not list is not listed, reads no chain for it, and opens no other', async () => {
    const unlisted = optionExpiryAnchorMs('2099-06-18');
    const fake = market();

    await renderChain(fake, { expiryMs: unlisted });

    expect(await screen.findByText("The 2099-06-18 expiry is not among the provider's current expirations.")).toBeTruthy();
    expect(fake.chain).not.toHaveBeenCalled();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
  });

  it('hands a pick in the ribbon up, and changes nothing itself: the address answers with the expiry', async () => {
    const picked = vi.fn();
    const fake = market();
    await renderChain(fake, { expiryMs: NEAR }, { expiryPicked: picked });
    await screen.findByRole('table', { name: /Calls on the left/ });

    await userEvent.setup().click(screen.getByRole('button', { name: '19' }));

    expect(picked).toHaveBeenCalledWith(FAR);
    expect(fake.chain).toHaveBeenCalledTimes(1);
  });

  it('drops the previous expiry\'s quotes at once, and says so when the new chain fails', async () => {
    const fake = market();
    const view = await renderChain(fake, { expiryMs: NEAR });
    await screen.findByRole('table', { name: /Calls on the left/ });
    fake.chain.mockImplementation(() => from(Promise.reject(new Error('Snapshot unavailable'))));

    await view.rerender({ inputs: { expiryMs: FAR }, partialUpdate: true });

    expect(await screen.findByText('Snapshot unavailable')).toBeTruthy();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(facts()).toBe('');
  });
});

describe('what the snapshot says when it cannot be read', () => {
  it('shows the reason when the expirations lookup fails, not just an empty chain', async () => {
    const fake = market({ expirations: vi.fn(() => from(Promise.reject(new Error('Option expirations unavailable: Polygon rate limited')))) });

    await renderChain(fake, { expiryMs: NEAR });

    expect(await screen.findByText(/Polygon rate limited/)).toBeTruthy();
  });

  it('says the provider lists no expirations for an instrument that has none', async () => {
    await renderChain(market({ expirations: vi.fn(() => of([])) }), {});

    expect(await screen.findByText('The provider lists no option expirations for SPY.')).toBeTruthy();
  });
});

describe('the snapshot refreshing', () => {
  afterEach(() => vi.useRealTimers());

  async function renderPolling(fake: Market) {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const view = await renderChain(fake, { expiryMs: NEAR });
    await screen.findByRole('table', { name: /Calls on the left/ });
    return view;
  }

  it('marks the quotes stale, with the reason, when a background refresh fails, and clears the mark when one succeeds', async () => {
    const fake = market();
    await renderPolling(fake);
    expect(facts()).not.toContain('Stale');

    fake.chain.mockImplementationOnce(() => from(Promise.reject(new Error('Gateway timeout'))));
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await screen.findByText(/Stale: the last refresh failed \(Gateway timeout\)/)).toBeTruthy();
    // The quotes on screen stay, and no banner replaces them.
    expect(screen.getByRole('table', { name: /Calls on the left/ })).toBeTruthy();

    await vi.advanceTimersByTimeAsync(5_000);
    await waitFor(() => expect(facts()).not.toContain('Stale'));
  });

  it('does not start a second refresh while one is still pending', async () => {
    const fake = market();
    await renderPolling(fake);
    const slow = deferred<OptionsChainSnapshotResult>();
    fake.chain.mockImplementation(() => from(slow.promise));
    const before = fake.chain.mock.calls.length;

    await vi.advanceTimersByTimeAsync(5_000);
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.advanceTimersByTimeAsync(5_000);

    expect(fake.chain.mock.calls.length).toBe(before + 1);
    slow.resolve(chainReply(700));
  });

  it('stops with the component: nothing is read after the source is left', async () => {
    const fake = market();
    const view = await renderPolling(fake);
    const before = fake.chain.mock.calls.length;

    view.fixture.destroy();
    await vi.advanceTimersByTimeAsync(20_000);

    expect(fake.chain.mock.calls.length).toBe(before);
  });
});

describe('an answer that arrives after the instrument or the expiry was changed', () => {
  it('is dropped: a reply for the instrument the operator has left writes nothing', async () => {
    const late = deferred<OptionsChainSnapshotResult>();
    const fake = market({ chain: vi.fn((ticker: string) => (ticker === 'SPY' ? from(late.promise) : of(chainReply(222, 480)))) });
    const view = await renderChain(fake, { expiryMs: NEAR });
    await waitFor(() => expect(fake.chain).toHaveBeenCalledWith('SPY', NEAR));

    await view.rerender({ inputs: { symbol: 'QQQ' }, partialUpdate: true });
    late.resolve(chainReply(111, 999));

    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    await waitFor(() => expect(document.querySelectorAll('.strike-cell')).toHaveLength(1));
    expect(document.querySelector('.strike-cell')?.textContent?.trim()).toContain('222.00');
    expect(facts()).toContain('Underlying QQQ');
  });

  it('is dropped: a reply for the expiry the operator has left writes nothing', async () => {
    const late = deferred<OptionsChainSnapshotResult>();
    const fake = market({ chain: vi.fn((_ticker: string, expiry?: number) => (expiry === NEAR ? from(late.promise) : of(chainReply(333)))) });
    const view = await renderChain(fake, { expiryMs: NEAR });
    await waitFor(() => expect(fake.chain).toHaveBeenCalledWith('SPY', NEAR));

    await view.rerender({ inputs: { expiryMs: FAR }, partialUpdate: true });
    late.resolve(chainReply(111, 999));

    await waitFor(() => expect(document.querySelector('.strike-cell')?.textContent?.trim()).toContain('333.00'));
    await new Promise((done) => setTimeout(done, 0));
    expect(document.querySelectorAll('.strike-cell')).toHaveLength(1);
  });
});

describe('the density of the snapshot', () => {
  afterEach(() => localStorage.clear());

  it('is the one saved under the key the Options Lab chain wrote, and is saved there when it is changed', async () => {
    localStorage.setItem('optionsLab.chain.density', 'flow');
    await renderChain(market(), { expiryMs: NEAR });
    await screen.findByRole('table', { name: /Calls on the left/ });
    expect(screen.getByRole('button', { name: 'Flow' }).getAttribute('aria-pressed')).toBe('true');

    await userEvent.setup().click(screen.getByRole('button', { name: 'Greeks' }));

    expect(localStorage.getItem('optionsLab.chain.density')).toBe('greeks');
    expect(screen.getByRole('button', { name: 'Greeks' }).getAttribute('aria-pressed')).toBe('true');
  });

  it('limits the strikes to the range chosen, and shows every one on All', async () => {
    const contracts = Array.from({ length: 41 }, (_, index) => 680 + index).flatMap((strike) => [contractAt('call', strike), contractAt('put', strike)]);
    await renderChain(market({ chain: vi.fn(() => of(chainReply(0, 700, contracts))) }), { expiryMs: NEAR });
    await screen.findByRole('table', { name: /Calls on the left/ });
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: '±5' }));
    expect(document.querySelectorAll('.strike-cell')).toHaveLength(11);
    await user.click(screen.getByRole('button', { name: 'All' }));

    expect(document.querySelectorAll('.strike-cell')).toHaveLength(41);
  });
});
