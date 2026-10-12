import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../testing/page-actions';
import type { DownloadAnswer } from './past-chain.types';
import { deferred } from './testing/deferred';
import { chainDay, downloadAnswer, item, TRADING_DATE, TRADING_DATE_MS } from './testing/fixtures';
import { fakeChainService, pickDate, showChain } from './testing/render-chain';
import { renderPageWithChain, symbolPickers } from './testing/render-chain-page';

const tab = (name: string) => screen.getByRole('tab', { name });
const openChainTab = () => fireEvent.click(tab('Past chain'));
const showBacktest = () => fireEvent.click(tab('Backtest'));

describe('the Past chain as a tab of the page', () => {
  it('is the third tab, and nothing of it is made, or read, until it is opened', async () => {
    const chain = fakeChainService(vi);
    await renderPageWithChain(vi, chain);

    expect(screen.getAllByRole('tab').map((each) => each.textContent?.trim())).toEqual(['Backtest', 'History', 'Past chain']);
    expect(document.querySelector('app-options-backtest-past-chain')).toBeNull();
    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });

    expect(document.querySelectorAll('app-options-backtest-past-chain')).toHaveLength(1);
    expect(chain.day).not.toHaveBeenCalled();
    expect(tab('Past chain').getAttribute('aria-selected')).toBe('true');
  });

  it('has no bar of setup actions over it: Save, Check data and Start belong to the Backtest tab', async () => {
    await renderPageWithChain(vi, fakeChainService(vi));

    openChainTab();
    await screen.findByRole('heading', { name: 'Past chain' });

    // Not in the page at all: a bar that were only hidden would be fixed to the window and still be there for the keyboard.
    expect(screen.queryByRole('button', { name: 'Start backtest', hidden: true })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Check data', hidden: true })).toBeNull();
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
