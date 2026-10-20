import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { checkButton, lettingGo, openExample, pickDates, reads } from '../testing/page-actions';
import { fakeService } from '../testing/render-page';
import { PastChainRefusedError } from './past-chain.service';
import type { DownloadAnswer } from './past-chain.types';
import { deferred } from './testing/deferred';
import { chainDay, downloadAnswer, item, TRADING_DATE, TRADING_DATE_MS } from './testing/fixtures';
import { fakeChainService, pickDate, showChain, typeStrikes, type FakeChainService } from './testing/render-chain';
import { renderPageWithChain, symbolPickers } from './testing/render-chain-page';

const tab = (name: string) => screen.getByRole('tab', { name });
const openChainTab = () => fireEvent.click(tab('Past chain'));
const showBacktest = () => fireEvent.click(tab('Backtest'));

describe('the Past chain as a tab of the page', () => {
  it('is the third tab, nothing of it is made, or read, until it is opened, and it has no bar of setup actions over it: Save, Check data and Start belong to the Backtest tab', async () => {
    const chain = fakeChainService(vi);
    await renderPageWithChain(vi, chain);

    expect.soft(screen.getAllByRole('tab').map((each) => each.textContent?.trim()), 'is the third tab').toEqual(['Backtest', 'History', 'Past chain']);
    expect.soft(document.querySelector('app-options-backtest-past-chain'), 'is not made until it is opened').toBeNull();
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });

    expect.soft(document.querySelectorAll('app-options-backtest-past-chain'), 'is made once when it is opened').toHaveLength(1);
    expect.soft(chain.day, 'is not read when the tab is opened, before a date is picked').not.toHaveBeenCalled();
    expect.soft(tab('Past chain').getAttribute('aria-selected'), 'is the selected tab once opened').toBe('true');
    // Not in the page at all: a bar that were only hidden would be fixed to the window and still be there for the keyboard.
    expect.soft(screen.queryByRole('button', { name: 'Start backtest', hidden: true }), 'has no Start button of a bar of setup actions over it').toBeNull();
    expect.soft(screen.queryByRole('button', { name: 'Check data', hidden: true }), 'has no Check data button of a bar of setup actions over it').toBeNull();
  });

  it('does not start a backtest from the Shift+Enter shortcut while it is shown, and does not take the key', async () => {
    const service = fakeService(vi);
    await renderPageWithChain(vi, fakeChainService(vi), service);
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' }).disabled).toBe(false));
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });

    const event = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });
    document.body.dispatchEvent(event);
    await lettingGo();

    expect(service.startRun).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it('is handed the server’s limits by the page, as the setup’s cards are: the strikes box says the most it takes and refuses a larger count', async () => {
    const service = fakeService(vi);
    const chain = fakeChainService(vi);
    await renderPageWithChain(vi, chain, service);
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await screen.findByRole('table', { name: /Calls on the left/ });

    expect(service.limits).toHaveBeenCalled();
    await waitFor(() => expect(reads(document.getElementById('ob-chain-strikes-note'))).toBe("Empty means the server's own number. The most it takes: 14."));
    typeStrikes('15');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Strikes each side of the money: the most the server takes is 14. Enter 14 or fewer, or leave the box empty.');
    expect(chain.expiry.mock.calls.map((call) => call[0].strikes)).not.toContain('15');
  });

  it('is made once and kept: leaving it and coming back finds the date, the expiry and the chain where they were', async () => {
    const chain = fakeChainService(vi);
    await renderPageWithChain(vi, chain);
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await screen.findByRole('table', { name: /Calls on the left/ });

    showBacktest();
    openChainTab();

    expect(document.querySelectorAll('app-options-backtest-past-chain')).toHaveLength(1);
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
    expect(screen.getByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(chain.day).toHaveBeenCalledTimes(1);
    expect(chain.expiry).toHaveBeenCalledTimes(1);
  });

  it('has no accessibility violation with a chain open', async () => {
    await renderPageWithChain(vi, fakeChainService(vi));
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await screen.findByRole('table', { name: /Calls on the left/ });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`)).toEqual([]);
  });
});

describe('the underlying of the tab and the underlying of the setup', () => {
  it('starts from the setup when the tab is first opened', async () => {
    const chain = fakeChainService(vi);
    const view = await renderPageWithChain(vi, chain);
    symbolPickers(view.fixture)[0]?.symbol.set('AAPL');
    await view.fixture.whenStable();

    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    pickDate(TRADING_DATE);

    await waitFor(() => expect(chain.day).toHaveBeenCalledTimes(1));
    expect(chain.day.mock.calls[0]?.[0]).toEqual({ underlying: 'AAPL', tradingDateMs: TRADING_DATE_MS });
    expect(symbolPickers(view.fixture)[1]?.symbol()).toBe('AAPL');
  });

  it('is not the setup own: changing it on the tab leaves the setup as it was', async () => {
    const view = await renderPageWithChain(vi, fakeChainService(vi));
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    expect(symbolPickers(view.fixture)[0]?.symbol()).toBe('SPY');

    symbolPickers(view.fixture)[1]?.symbol.set('QQQ');
    await view.fixture.whenStable();
    showBacktest();

    expect(symbolPickers(view.fixture)[0]?.symbol()).toBe('SPY');
    expect(symbolPickers(view.fixture)[1]?.symbol()).toBe('QQQ');
  });

  it('is not moved by the setup: changing the setup once the tab is made leaves the tab as it was', async () => {
    const chain = fakeChainService(vi);
    const view = await renderPageWithChain(vi, chain);
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    showBacktest();

    symbolPickers(view.fixture)[0]?.symbol.set('IWM');
    await view.fixture.whenStable();
    openChainTab();
    pickDate(TRADING_DATE);

    await waitFor(() => expect(chain.day).toHaveBeenCalledTimes(1));
    expect(chain.day.mock.calls[0]?.[0]).toMatchObject({ underlying: 'SPY' });
    expect(symbolPickers(view.fixture)[1]?.symbol()).toBe('SPY');
    expect(symbolPickers(view.fixture)[0]?.symbol()).toBe('IWM');
  });
});

describe('a download on this tab and an answer of Check data', () => {
  /** Checks the data of a range, then downloads the listing of one date on the Past chain tab, and comes back to the Backtest tab. */
  async function downloadAfterACheck(chain: FakeChainService): Promise<void> {
    await renderPageWithChain(vi, chain);
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByRole('region', { name: 'Check data' });
    expect(screen.queryByText(/have changed since this check/)).toBeNull();
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    pickDate(TRADING_DATE);
    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));
    await waitFor(() => expect(chain.download).toHaveBeenCalledTimes(1));
    for (let settled = 0; settled < 5; settled += 1) await lettingGo();
    showBacktest();
  }

  it('makes the answer stale: what is stored has changed since it was asked', async () => {
    const chain = fakeChainService(vi, {
      day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })),
      download: vi.fn(async () => downloadAnswer([item('listing', 'SPY', 'fetched')])),
    });

    await downloadAfterACheck(chain);

    expect(await screen.findByText(/data has been downloaded/)).toBeTruthy();
    expect(screen.getByText(/have changed since this check/)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Check data' })).toBeTruthy();
  });

  it('is not made stale by a download the server refused before it fetched anything', async () => {
    const chain = fakeChainService(vi, {
      day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })),
      download: vi.fn(async () => {
        throw new PastChainRefusedError('OPTIONS_BACKTEST_TOO_MANY_CONTRACTS', 'a download takes at most 60 contracts; 61 were named', 400);
      }),
    });

    await downloadAfterACheck(chain);
    await lettingGo();

    expect(screen.queryByText(/have changed since this check/)).toBeNull();
  });
});

describe('a download and the tab being left', () => {
  it('is not cancelled by leaving the tab: it is asked once, and its answer is there on return', async () => {
    const answer = deferred<DownloadAnswer>();
    const chain = fakeChainService(vi, {
      day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })),
      download: vi.fn(() => answer.promise),
    });
    await renderPageWithChain(vi, chain);
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });
    pickDate(TRADING_DATE);
    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Download missing data' }).disabled).toBe(true));

    showBacktest();
    answer.resolve(downloadAnswer([item('listing', 'SPY', 'fetched')]));
    await answer.promise;
    openChainTab();

    expect(await screen.findByRole('group', { name: 'Download result' })).toBeTruthy();
    expect(chain.download).toHaveBeenCalledTimes(1);
    expect(reads(within(screen.getByRole('group', { name: 'Download result' })).getByRole('status'))).toBe('1 fetched, 0 already stored, 0 failed.');
  });
});
