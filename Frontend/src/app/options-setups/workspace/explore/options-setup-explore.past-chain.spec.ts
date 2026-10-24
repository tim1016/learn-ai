import { By } from '@angular/platform-browser';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { PastChainRefusedError } from '../../../components/options-backtest/past-chain/past-chain.service';
import type { DownloadAnswer } from '../../../components/options-backtest/past-chain/past-chain.types';
import { deferred } from '../../../components/options-backtest/past-chain/testing/deferred';
import { chainDay, downloadAnswer, item, TRADING_DATE, TRADING_DATE_MS } from '../../../components/options-backtest/past-chain/testing/fixtures';
import { fakeChainService, pickDate, showChain, typeStrikes, type FakeChainService } from '../../../components/options-backtest/past-chain/testing/render-chain';
import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { EXAMPLE_ONE_REENTRY, exampleBody, servedExamples, servedSaved } from '../../../components/options-backtest/testing/fixtures';
import { checkButton, lettingGo, pickDates, reads } from '../../../components/options-backtest/testing/page-actions';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import { goToDefine, goToExplore } from '../../testing/explore-actions';
import { renderExplore } from '../../testing/render-workspace';

const viewLink = (name: string) => within(screen.getByRole('navigation', { name: 'Explore views' })).getByRole('link', { name });
const openChain = () => fireEvent.click(viewLink('Past chain'));
const showBacktest = () => fireEvent.click(viewLink('Backtest'));
const chainHeading = () => screen.findByRole('heading', { name: 'Past chain' });
const symbolPickers = (view: Awaited<ReturnType<typeof renderExplore>>) =>
  view.fixture.debugElement.queryAll(By.directive(SymbolPickerComponent)).map((each) => each.componentInstance as SymbolPickerComponent);

describe('the Past chain as a view of Explore', () => {
  it('is the third view, nothing of it is made, or read, until it is opened, and the run actions are not over it', async () => {
    const chain = fakeChainService(vi);
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { chain });

    expect.soft(within(screen.getByRole('navigation', { name: 'Explore views' })).getAllByRole('link').map((each) => each.textContent?.trim()).slice(0, 3), 'is the third view').toEqual(['Backtest', 'History', 'Past chain']);
    expect.soft(document.querySelector('app-options-backtest-past-chain'), 'is not made until it is opened').toBeNull();
    openChain();
    await chainHeading();

    expect.soft(document.querySelectorAll('app-options-backtest-past-chain'), 'is made once when it is opened').toHaveLength(1);
    expect.soft(chain.day, 'is not read when the view is opened, before a date is picked').not.toHaveBeenCalled();
    expect.soft(viewLink('Past chain').getAttribute('aria-current'), 'is the open view once opened').toBe('page');
    // Not in the page at all: a set of run actions that were only hidden would still be there for the keyboard.
    expect.soft(screen.queryByRole('button', { name: 'Start backtest', hidden: true }), 'has no Start button of the run actions over it').toBeNull();
    expect.soft(screen.queryByRole('button', { name: 'Check data', hidden: true }), 'has no Check data button of the run actions over it').toBeNull();
  });

  it('does not start a backtest from the Shift+Enter shortcut while it is shown, and does not take the key', async () => {
    const service = fakeService(vi);
    await renderExplore(service, EXAMPLE_ONE_REENTRY, { chain: fakeChainService(vi) });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' }).disabled).toBe(false));
    openChain();
    await chainHeading();

    const event = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });
    document.body.dispatchEvent(event);
    await lettingGo();

    expect(service.startRun).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it('is handed the server’s limits by the page, as the setup’s cards are: the strikes box says the most it takes and refuses a larger count', async () => {
    const service = fakeService(vi);
    const chain = fakeChainService(vi);
    await renderExplore(service, EXAMPLE_ONE_REENTRY, { chain });
    openChain();
    await chainHeading();
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
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { chain });
    openChain();
    await chainHeading();
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await screen.findByRole('table', { name: /Calls on the left/ });

    showBacktest();
    await screen.findByRole('heading', { name: 'Duration' });
    openChain();
    await chainHeading();

    expect(document.querySelectorAll('app-options-backtest-past-chain')).toHaveLength(1);
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
    expect(screen.getByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(chain.day).toHaveBeenCalledTimes(1);
    expect(chain.expiry).toHaveBeenCalledTimes(1);
  });

  it('is not shown while another view is, though it is kept', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { chain: fakeChainService(vi) });
    openChain();
    await chainHeading();

    showBacktest();
    await screen.findByRole('heading', { name: 'Duration' });

    expect(document.querySelector('app-options-backtest-past-chain')?.parentElement?.hidden).toBe(true);
  });

  it('has no accessibility violation with a chain open', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { chain: fakeChainService(vi) });
    openChain();
    await chainHeading();
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await screen.findByRole('table', { name: /Calls on the left/ });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`)).toEqual([]);
  });
});

describe('the underlying of the view and the underlying of the setup', () => {
  const library = () => [...servedExamples(), servedSaved('Apple', { ...exampleBody(), underlying: 'AAPL' })];
  const service = () => fakeService(vi, { list: vi.fn(async () => library()) });

  it('starts from the saved setup when the view is first opened', async () => {
    const chain = fakeChainService(vi);
    const view = await renderExplore(service(), 'Apple', { chain });

    openChain();
    await chainHeading();
    pickDate(TRADING_DATE);

    await waitFor(() => expect(chain.day).toHaveBeenCalledTimes(1));
    expect(chain.day.mock.calls[0]?.[0]).toEqual({ underlying: 'AAPL', tradingDateMs: TRADING_DATE_MS });
    expect(symbolPickers(view)[0]?.symbol()).toBe('AAPL');
  });

  it('is not the setup own: changing it on the view leaves the setup as it was', async () => {
    const view = await renderExplore(service(), EXAMPLE_ONE_REENTRY, { chain: fakeChainService(vi) });
    openChain();
    await chainHeading();
    expect(symbolPickers(view)[0]?.symbol()).toBe('SPY');

    symbolPickers(view)[0]?.symbol.set('QQQ');
    await view.fixture.whenStable();
    await goToDefine();

    // Define's own instrument is the setup's, and is still SPY.
    expect(symbolPickers(view).map((picker) => picker.symbol())).toContain('SPY');
    expect(symbolPickers(view).map((picker) => picker.symbol())).not.toContain('QQQ');
  });

  it('starts from the saved setup, never from a draft being edited in Define', async () => {
    const chain = fakeChainService(vi);
    const view = await renderExplore(service(), EXAMPLE_ONE_REENTRY, { chain });
    openChain();
    await chainHeading();
    await goToDefine();

    symbolPickers(view)[0]?.symbol.set('IWM');
    await view.fixture.whenStable();
    await goToExplore();
    openChain();
    await chainHeading();
    pickDate(TRADING_DATE);

    await waitFor(() => expect(chain.day).toHaveBeenCalledTimes(1));
    expect(chain.day.mock.calls[0]?.[0]).toMatchObject({ underlying: 'SPY' });
  });
});

describe('a download on this view and an answer of Check data', () => {
  /** Checks the data of a range, then downloads the listing of one date on the Past chain view, and comes back to Backtest. */
  async function downloadAfterACheck(chain: FakeChainService): Promise<void> {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { chain });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(checkButton().disabled).toBe(false));
    fireEvent.click(checkButton());
    await screen.findByRole('region', { name: 'Check data' });
    expect(screen.queryByText(/have changed since this check/)).toBeNull();
    openChain();
    await chainHeading();
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

describe('a download and the view being left', () => {
  it('is not cancelled by leaving the view: it is asked once, and its answer is there on return', async () => {
    const answer = deferred<DownloadAnswer>();
    const chain = fakeChainService(vi, {
      day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })),
      download: vi.fn(() => answer.promise),
    });
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY, { chain });
    openChain();
    await chainHeading();
    pickDate(TRADING_DATE);
    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Download missing data' }).disabled).toBe(true));

    showBacktest();
    answer.resolve(downloadAnswer([item('listing', 'SPY', 'fetched')]));
    await answer.promise;
    openChain();

    expect(await screen.findByRole('group', { name: 'Download result' })).toBeTruthy();
    expect(chain.download).toHaveBeenCalledTimes(1);
    expect(reads(within(screen.getByRole('group', { name: 'Download result' })).getByRole('status'))).toBe('1 fetched, 0 already stored, 0 failed.');
  });
});
