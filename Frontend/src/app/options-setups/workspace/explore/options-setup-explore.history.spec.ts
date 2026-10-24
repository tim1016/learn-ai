import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { EXAMPLE_ONE_REENTRY, exampleBody, servedSaved, servedExamples } from '../../../components/options-backtest/testing/fixtures';
import { dateFields, lettingGo, pickDates, press } from '../../../components/options-backtest/testing/page-actions';
import { runDetail, runItem, runWithoutSetup, SETUP_CANNOT_OPEN_REASON } from '../../../components/options-backtest/testing/run-fixtures';
import { scriptedRun } from '../../../components/options-backtest/testing/run-script';
import { goToDefine, goToExplore } from '../../testing/explore-actions';
import { renderExplore } from '../../testing/render-workspace';

const viewLink = (name: string) => within(screen.getByRole('navigation', { name: 'Explore views' })).getByRole('link', { name });
const showHistory = () => fireEvent.click(viewLink('History'));
const showBacktest = () => fireEvent.click(viewLink('Backtest'));
const startButton = () => screen.queryByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
const url = () => decodeURIComponent(TestBed.inject(Router).url);

const COMPLETE = runItem({ id: 30, job_id: 'job-30', status: 'complete', setup_name: 'Straddle', headline: { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: '1234.56', ending_cash_usd: '11234.56' } });

describe('the views', () => {
  it('are Backtest, History, and the three that are about one run, and Backtest is the one shown', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);

    const nav = within(screen.getByRole('navigation', { name: 'Explore views' }));
    expect(nav.getAllByRole('link').map((each) => each.textContent?.trim())).toEqual(['Backtest', 'History', 'Performance', 'Research lineage', 'Historical comparison', 'Past chain in Markets']);
    expect(nav.getByRole('link', { name: 'Backtest' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('heading', { name: 'Duration' })).toBeTruthy();
  });

  it('hold no historical chain: the Past chain link leaves for the setup\'s underlying on the instrument\'s chain, Past · Archive', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);

    const link = within(screen.getByRole('navigation', { name: 'Explore views' })).getByRole('link', { name: 'Past chain in Markets' });
    expect(link.getAttribute('href')).toBe('/markets/SPY/options/chain?source=archive');
    expect(link.getAttribute('aria-current')).toBeNull();
    expect(document.querySelector('app-options-backtest-past-chain')).toBeNull();
  });

  it('do not make History, nor read its runs, until it is shown, and read them each time it is', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) });
    await renderExplore(service, EXAMPLE_ONE_REENTRY);
    const atStart = service.runs.mock.calls.length;
    expect(document.querySelector('app-options-backtest-history')).toBeNull();

    showHistory();
    await screen.findByRole('button', { name: 'Open run 30' });
    expect(service.runs.mock.calls.length).toBe(atStart + 1);
    expect(document.querySelectorAll('app-options-backtest-history')).toHaveLength(1);
    showBacktest();
    await screen.findByRole('heading', { name: 'Duration' });
    showHistory();

    await waitFor(() => expect(service.runs.mock.calls.length).toBe(atStart + 2));
  });

  it('keep the dates while History is shown', async () => {
    await renderExplore(fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) }), EXAMPLE_ONE_REENTRY);
    pickDates('2026-09-14', '2026-09-18');

    showHistory();
    await screen.findByRole('button', { name: 'Open run 30' });
    showBacktest();
    await screen.findByRole('heading', { name: 'Duration' });

    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
  });

  it('show the run actions on Backtest only', async () => {
    await renderExplore(fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) }), EXAMPLE_ONE_REENTRY);
    expect(screen.getByRole('region', { name: 'Run actions' })).toBeTruthy();

    showHistory();
    await screen.findByRole('button', { name: 'Open run 30' });

    expect(screen.queryByRole('region', { name: 'Run actions', hidden: true })).toBeNull();
    showBacktest();
    expect(await screen.findByRole('region', { name: 'Run actions' })).toBeTruthy();
  });

  it('do not start a backtest from the shortcut while History is shown', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) });
    await renderExplore(service, EXAMPLE_ONE_REENTRY);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' }).disabled).toBe(false));
    showHistory();
    await screen.findByRole('button', { name: 'Open run 30' });

    const event = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });
    document.body.dispatchEvent(event);
    await lettingGo();

    expect(service.startRun).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });
});

describe('Use this setup', () => {
  const ran = () => ({ ...exampleBody(), underlying: 'AAPL', slippage_percent: '0.45', starting_cash_usd: '25000.50' });
  const library = () => [...servedExamples(), servedSaved('Mine', exampleBody())];

  async function useTheSetupOfRun30() {
    const service = fakeService(vi, { list: vi.fn(async () => library()), runs: vi.fn(async () => [COMPLETE]), run: vi.fn(async () => runDetail({ ...COMPLETE, setup: ran() })) });
    await renderExplore(service, 'Mine');
    showHistory();
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByText('Window for AAPL.');
    press('Use this setup');
    return service;
  }

  it('puts the setup of the run into Define as a draft with no saved name, and shows Define', async () => {
    await useTheSetupOfRun30();

    await screen.findByRole('heading', { name: 'Entry settings' });
    await waitFor(() => expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.45'));
    expect((screen.getByLabelText('Cash') as HTMLInputElement).value).toBe('25000.50');
    expect(screen.getByRole('article', { name: 'Leg 2' })).toBeTruthy();
    expect(screen.getByText('The setup of that run was loaded as a new draft. It is not saved under any name.')).toBeTruthy();
    expect(url()).toBe('/options-setups/_new/define');
    expect(screen.getByText('Not saved')).toBeTruthy();
  });

  it('replaces the saved name with a generated name, because the draft is no longer that setup, and leaves the saved setup as it was', async () => {
    const service = await useTheSetupOfRun30();

    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toContain('AAPL, Buy'));
    expect(service.save).not.toHaveBeenCalled();
    expect(service.remove).not.toHaveBeenCalled();
  });

  it('cannot be pressed for a run whose setup the server could not open: it says why, and the draft is not touched', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => library()), runs: vi.fn(async () => [COMPLETE]), run: vi.fn(async () => runWithoutSetup({ ...COMPLETE })) });
    await renderExplore(service, 'Mine');
    showHistory();
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByText(SETUP_CANNOT_OPEN_REASON);
    const describedBefore = service.describe.mock.calls.length;

    const use = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this setup' });
    use.click();
    await lettingGo();

    expect(use.disabled).toBe(true);
    expect(url()).toBe('/options-setups/Mine/explore?view=history&run=30');
    expect(service.describe.mock.calls.length).toBe(describedBefore);
    await goToDefine();
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.3');
  });
});

describe('deleting the run the Backtest view shows', () => {
  it('takes it off the Backtest view, and leaves the view alone when another run is deleted', async () => {
    const setup = scriptedRun(vi);
    setup.service.run.mockResolvedValue(runDetail());
    await renderExplore(setup.service, EXAMPLE_ONE_REENTRY, { jobs: setup.jobs });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton()?.disabled).toBe(false));
    fireEvent.click(startButton() as HTMLButtonElement);
    await screen.findByRole('region', { name: 'Backtest run' });
    setup.jobs.emit('job-1', { type: 'job.completed', result_url: '/x' });
    await screen.findByRole('region', { name: 'Headline figures' });
    setup.service.runs.mockResolvedValue([runItem({ id: 8, job_id: 'job-8', status: 'complete' }), runItem({ id: 7, status: 'complete' })]);
    showHistory();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete run 8' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    await waitFor(() => expect(setup.service.deleteRun).toHaveBeenCalledWith(8));
    showBacktest();
    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();

    showHistory();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete run 7' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    await waitFor(() => expect(setup.service.deleteRun).toHaveBeenCalledWith(7));
    showBacktest();

    await waitFor(() => expect(screen.queryByRole('region', { name: 'Headline figures' })).toBeNull());
    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
  }, 15_000);

  it('is the same run whether Backtest is left for Define or for History', async () => {
    const setup = scriptedRun(vi);
    setup.service.run.mockResolvedValue(runDetail());
    await renderExplore(setup.service, EXAMPLE_ONE_REENTRY, { jobs: setup.jobs });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton()?.disabled).toBe(false));
    fireEvent.click(startButton() as HTMLButtonElement);
    await screen.findByRole('region', { name: 'Backtest run' });

    await goToDefine();
    await goToExplore();

    expect(await screen.findByRole('region', { name: 'Backtest run' })).toBeTruthy();
    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
  });
});
