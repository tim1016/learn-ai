import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeJobs } from './testing/fake-jobs';
import { exampleBody } from './testing/fixtures';
import { dateFields, lettingGo, openExample, pickDates, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';
import { runDetail, runItem, runWithoutSetup, SETUP_CANNOT_OPEN_REASON } from './testing/run-fixtures';
import { scriptedRun } from './testing/run-script';

const tab = (name: string) => screen.getByRole('tab', { name });
const showHistory = () => fireEvent.click(tab('History'));
const showBacktest = () => fireEvent.click(tab('Backtest'));
const startButton = () => screen.queryByRole<HTMLButtonElement>('button', { name: 'Start backtest' });

const COMPLETE = runItem({ id: 30, job_id: 'job-30', status: 'complete', setup_name: 'Straddle', headline: { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: '1234.56', ending_cash_usd: '11234.56' } });

describe('the two tabs', () => {
  it('are Backtest and History, and Backtest is the one shown', async () => {
    await renderPage(fakeService(vi));

    expect(screen.getAllByRole('tab').map((each) => each.textContent?.trim())).toEqual(['Backtest', 'History']);
    expect(tab('Backtest').getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Entry settings' })).toBeTruthy();
  });

  it('do not make History, nor read its runs, until it is shown, and read them each time it is', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) });
    await renderPage(service);
    const atStart = service.runs.mock.calls.length;
    expect(document.querySelector('app-options-backtest-history')).toBeNull();

    showHistory();
    await screen.findByRole('button', { name: 'Open run 30' });
    expect(service.runs.mock.calls.length).toBe(atStart + 1);
    expect(document.querySelectorAll('app-options-backtest-history')).toHaveLength(1);
    showBacktest();
    showHistory();

    await waitFor(() => expect(service.runs.mock.calls.length).toBe(atStart + 2));
  });

  it('keep what was typed in the setup while History is shown', async () => {
    await renderPage(fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) }));
    typeInto('Slippage (percent)', '0.45');
    pickDates('2026-09-14', '2026-09-18');

    showHistory();
    await screen.findByRole('button', { name: 'Open run 30' });
    showBacktest();

    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.45');
    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
  });

  it('show the bar of actions on Backtest only', async () => {
    await renderPage(fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) }));
    expect(screen.getByRole('region', { name: 'Setup actions' })).toBeTruthy();

    showHistory();
    await screen.findByRole('button', { name: 'Open run 30' });

    // Not merely hidden with its tab: the bar is fixed to the window, so it must not be there to show.
    expect(screen.queryByRole('region', { name: 'Setup actions', hidden: true })).toBeNull();
    showBacktest();
    expect(await screen.findByRole('region', { name: 'Setup actions' })).toBeTruthy();
  });

  it('do not start a backtest from the shortcut while History is shown', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [COMPLETE]) });
    await renderPage(service);
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton()?.disabled).toBe(false));
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

  async function useTheSetupOfRun30() {
    const service = fakeService(vi, { runs: vi.fn(async () => [COMPLETE]), run: vi.fn(async () => runDetail({ ...COMPLETE, setup: ran() })) });
    const jobs = fakeJobs();
    await renderPage(service, undefined, { jobs });
    pickDates('2026-09-14', '2026-09-18');
    showHistory();
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByText('Window for AAPL.');
    press('Use this setup');
    return service;
  }

  it('puts the setup of the run into the form of the Backtest tab, and shows that tab', async () => {
    await useTheSetupOfRun30();

    await waitFor(() => expect(tab('Backtest').getAttribute('aria-selected')).toBe('true'));
    await waitFor(() => expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.45'));
    expect((screen.getByLabelText('Cash') as HTMLInputElement).value).toBe('25000.50');
    expect(screen.getByRole('article', { name: 'Leg 2' })).toBeTruthy();
    expect(screen.getByText('The setup of that run was loaded. The dates were not changed.')).toBeTruthy();
  });

  it('leaves the dates as they were, and empties the name and the highlight of a saved setup, because the form is no longer that setup', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [COMPLETE]), run: vi.fn(async () => runDetail({ ...COMPLETE, setup: ran() })) });
    await renderPage(service);
    await openExample();
    typeInto('Setup name', 'Typed');
    pickDates('2026-09-14', '2026-09-18');
    const side = within(screen.getByRole('complementary', { name: 'Saved setups' }));
    expect(side.getAllByRole('button').filter((button) => button.getAttribute('aria-current') === 'true')).toHaveLength(1);
    showHistory();
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByText('Window for AAPL.');

    press('Use this setup');

    await waitFor(() => expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.45'));
    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
    expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('');
    expect(within(screen.getByRole('complementary', { name: 'Saved setups' })).getAllByRole('button').filter((button) => button.getAttribute('aria-current') === 'true')).toHaveLength(0);
  });

  it('cannot be pressed for a run whose setup the server could not open: it says why, and the form is not touched', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [COMPLETE]), run: vi.fn(async () => runWithoutSetup({ ...COMPLETE })) });
    await renderPage(service);
    typeInto('Slippage (percent)', '0.45');
    showHistory();
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByText(SETUP_CANNOT_OPEN_REASON);
    const describedBefore = service.describe.mock.calls.length;

    const use = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this setup' });
    use.click();
    await lettingGo();

    expect(use.disabled).toBe(true);
    expect(tab('History').getAttribute('aria-selected')).toBe('true');
    expect(service.describe.mock.calls.length).toBe(describedBefore);
    expect(service.describe.mock.calls.every(([body]) => body !== null)).toBe(true);
    showBacktest();
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.45');
  });

  it('starts a run under no name, because the saved setup of that name may have changed since', async () => {
    const service = await useTheSetupOfRun30();
    await waitFor(() => expect(startButton()?.disabled).toBe(false));

    fireEvent.click(startButton() as HTMLButtonElement);

    await waitFor(() => expect(service.startRun).toHaveBeenCalledTimes(1));
    expect(service.startRun.mock.calls[0]?.[0].setup_name).toBeNull();
    expect(service.startRun.mock.calls[0]?.[0].setup).toMatchObject({ underlying: 'AAPL', slippage_percent: '0.45' });
  });
});

describe('deleting the run the Backtest tab shows', () => {
  it('takes it off the Backtest tab, and leaves the tab alone when another run is deleted', async () => {
    const setup = scriptedRun(vi);
    setup.service.run.mockResolvedValue(runDetail());
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    await openExample();
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
    expect(screen.getByRole('region', { name: 'Headline figures' })).toBeTruthy();

    showHistory();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete run 7' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    await waitFor(() => expect(setup.service.deleteRun).toHaveBeenCalledWith(7));
    showBacktest();

    await waitFor(() => expect(screen.queryByRole('region', { name: 'Headline figures' })).toBeNull());
    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
  });
});
