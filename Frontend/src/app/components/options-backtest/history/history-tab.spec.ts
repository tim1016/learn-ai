import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { RunRefusedError } from '../options-backtest.service';
import type { RunListItem } from '../options-backtest.types';
import { fakeJobs, type FakeJobs } from '../testing/fake-jobs';
import { expectTradingDatesInNewYork, lettingGo, press, reads } from '../testing/page-actions';
import { fakeService, type FakeService } from '../testing/render-page';
import { renderHistoryTab } from '../testing/render-history';
import { runDetail, runItem, runWithoutSetup, SETUP_CANNOT_OPEN_REASON } from '../testing/run-fixtures';
import { sessionOpenMs } from '../../../shared/date-picker/testing/fake-trading-calendar';

const HEADLINE = { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: '1234.56', ending_cash_usd: '11234.56' };

const COMPLETE = runItem({ id: 30, job_id: 'job-30', status: 'complete', setup_name: 'Straddle', underlying: 'AAPL', headline: HEADLINE, finished_at_ms: 1 });
const RUNNING = runItem({ id: 31, job_id: 'job-31', status: 'running' });
const FAILED = runItem({ id: 29, job_id: 'job-29', status: 'failed', failure: { code: 'DATA_MISSING', reason: 'Some sessions have no data.', items: [{ session_open_ms: sessionOpenMs('2026-09-15'), code: 'listing', message: 'No listing was stored.' }] } });
const CANCELLED = runItem({ id: 28, job_id: 'job-28', status: 'cancelled' });
const INTERRUPTED = runItem({ id: 27, job_id: 'job-27', status: 'interrupted' });
const RUNS = [RUNNING, COMPLETE, FAILED, CANCELLED, INTERRUPTED];

async function renderHistory(options: { service?: FakeService; jobs?: FakeJobs; active?: boolean } = {}) {
  return renderHistoryTab(vi, { service: options.service ?? fakeService(vi, { runs: vi.fn(async () => RUNS) }), jobs: options.jobs, active: options.active });
}

const rowFor = (id: number) => screen.getByRole('button', { name: `Open run ${id}` }).closest('tr') as HTMLElement;

describe('the list of runs', () => {
  it('lists the runs newest first with their start, end, setup, dates, headline figures and status', async () => {
    await renderHistory();

    await screen.findAllByRole('row');

    const ids = screen.getAllByRole('button', { name: /^Open run / }).map((button) => button.getAttribute('aria-label'));
    const row = rowFor(30);
    const cells = [...row.querySelectorAll('td')].map((cell) => reads(cell));

    expect.soft(ids, 'lists the runs in the order the server gave, newest first').toEqual(['Open run 31', 'Open run 30', 'Open run 29', 'Open run 28', 'Open run 27']);

    expect.soft(cells[0], 'says when each was started').toMatch(/^2026-09-2[12] \d\d:\d\d:\d\d$/);
    expect.soft(cells.slice(1, 4), 'says how it ended, the setup’s name and underlying, and the dates').toEqual(['Complete', 'AAPL Straddle', '2026-09-14 – 2026-09-18']);
    expect.soft(reads(rowFor(31).querySelectorAll('td')[2]), 'says the underlying of run 31').toBe('SPY');

    expect.soft(reads(rowFor(30).querySelectorAll('td')[4]), 'shows the headline figures of a complete run as the server wrote them').toBe('Net profit $1,234.56 Ending cash $11,234.56 Trades 12, won 7 Sessions traded 8 of 10');
    for (const id of [31, 29, 28, 27]) expect.soft(reads(rowFor(id).querySelectorAll('td')[4]), `shows no headline figures for run ${id}, which is not complete`).toBe('');

    expect.soft([31, 30, 29, 28, 27].map((id) => reads(rowFor(id).querySelectorAll('td')[1])), 'names every status in words').toEqual(['Running', 'Complete', 'Failed', 'Cancelled', 'Interrupted']);
    expectTradingDatesInNewYork(row.querySelectorAll('td')[3]);
  });

  it('says so when there are no runs', async () => {
    await renderHistory({ service: fakeService(vi, { runs: vi.fn(async () => []) }) });

    expect(await screen.findByText('No runs yet. Start a backtest on the Backtest tab.')).toBeTruthy();
  });

  it('says plainly when the runs cannot be read, and reads them again on request', async () => {
    let down = true;
    const runs = vi.fn(async () => {
      if (down) throw new Error('offline');
      return RUNS;
    });
    await renderHistory({ service: fakeService(vi, { runs }) });

    expect((await screen.findByText('The runs could not be read.')).getAttribute('role')).toBe('alert');
    down = false;
    press('Try again');

    await screen.findByRole('button', { name: 'Open run 31' });
  });

  it('is read again each time the tab becomes the one on show, and not while it is not', async () => {
    const { view, service } = await renderHistory({ active: false });
    await screen.findAllByRole('row');
    const afterFirst = service.runs.mock.calls.length;

    view.fixture.componentRef.setInput('active', true);
    view.fixture.detectChanges();
    await waitFor(() => expect(service.runs.mock.calls.length).toBe(afterFirst + 1));
    view.fixture.componentRef.setInput('active', false);
    view.fixture.detectChanges();
    await lettingGo();

    expect(service.runs.mock.calls.length).toBe(afterFirst + 1);
  });
});

describe('opening a run', () => {
  it('shows a run that is going as it goes, with Cancel', async () => {
    const jobs = fakeJobs();
    jobs.register('job-31');
    jobs.emit('job-31', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    jobs.emit('job-31', { type: 'job.progress', current: 2, total: 5, unit: 'sessions' });
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...RUNNING, status: 'running', figures: null, slippage: [], slippage_tolerance: null, headline: null })) });
    await renderHistory({ service, jobs });

    fireEvent.click(await screen.findByRole('button', { name: 'Open run 31' }));

    const panel = await screen.findByRole('region', { name: 'Backtest run' });
    expect(within(panel).getByText('Replaying each session')).toBeTruthy();
    expect(within(panel).getByText('Sessions done: 2 of 5')).toBeTruthy();
    expect(within(panel).getByRole('button', { name: 'Cancel' })).toBeTruthy();
    expect(service.run).toHaveBeenCalledWith(31);
  });

  it('opens a complete run: the keyboard on it, its banner and result, and the setup it used in the server’s sentences', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...COMPLETE })) });
    await renderHistory({ service });

    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));

    await waitFor(() => expect(document.activeElement, 'puts the keyboard on the run it opened').toBe(screen.getByRole('heading', { name: 'Backtest run' })));
    expect.soft(await screen.findByRole('note', { name: 'How these figures were made' }), 'shows a complete run’s banner').toBeTruthy();
    expect.soft(await screen.findByRole('region', { name: 'Headline figures' }), 'shows a complete run’s result').toBeTruthy();
    expect.soft(await screen.findByText('Window for SPY.'), 'shows the setup it used in the server’s sentences').toBeTruthy();
    expect.soft(service.describe, 'asks the server to describe the setup the run used').toHaveBeenCalledWith(runDetail().setup);
  });

  it('opens a complete run whose setup the server cannot open: the figures, the reason in the server’s words, and no way to use the setup', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runWithoutSetup({ ...COMPLETE })) });
    const { view } = await renderHistory({ service });
    const used: unknown[] = [];
    view.fixture.componentInstance.useSetup.subscribe((setup) => used.push(setup));

    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(await screen.findByText(SETUP_CANNOT_OPEN_REASON)).toBeTruthy();
    const use = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this setup' });
    expect(use.disabled).toBe(true);
    use.click();
    await lettingGo();
    expect(service.describe).not.toHaveBeenCalled();
    expect(used).toEqual([]);
  });

  it.each([
    [28, 'cancelled', 'The run was cancelled. Nothing was saved.'],
    [27, 'interrupted', 'This run did not finish, and its job is gone. It saved no result.'],
  ] as const)('says what became of run %i, which is %s', async (id, status, words) => {
    const item = RUNS.find((run) => run.id === id) as RunListItem;
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...item, status, figures: null, slippage: [], slippage_tolerance: null, headline: null })) });
    await renderHistory({ service });

    fireEvent.click(await screen.findByRole('button', { name: `Open run ${id}` }));

    expect(await screen.findByText(words)).toBeTruthy();
  });

  it('goes back to the list, read again, with the keyboard back on the Open button of the run', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...COMPLETE })) });
    await renderHistory({ service });
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByRole('region', { name: 'Headline figures' });
    const before = service.runs.mock.calls.length;

    press('Back to the runs');

    await screen.findByRole('button', { name: 'Open run 30' });
    expect.soft(screen.queryByRole('region', { name: 'Backtest run' }), 'goes back to the list').toBeNull();
    expect.soft(service.runs.mock.calls.length, 'reads the list again').toBe(before + 1);
    await waitFor(() => expect(document.activeElement, 'puts the keyboard back on the Open button of the run, when the owner goes back to the list').toBe(screen.getByRole('button', { name: 'Open run 30' })));
  });

  it('gives the setup of the run back when Use this setup is pressed', async () => {
    const detail = runDetail({ ...COMPLETE });
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => detail) });
    const { view } = await renderHistory({ service });
    const used: unknown[] = [];
    view.fixture.componentInstance.useSetup.subscribe((setup) => used.push(setup));
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByText('Window for SPY.');

    press('Use this setup');

    expect(used).toEqual([detail.setup]);
  });
});

describe('deleting a run', () => {
  it('asks once, then deletes it by its id and reads the list again', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS) });
    await renderHistory({ service });
    await screen.findByRole('button', { name: 'Open run 30' });
    const before = service.runs.mock.calls.length;

    press('Delete run 30');
    expect(service.deleteRun).not.toHaveBeenCalled();
    within(rowFor(30)).getByRole('group', { name: 'Confirm deletion' });
    press('Confirm delete');

    await waitFor(() => expect(service.deleteRun).toHaveBeenCalledWith(30));
    await waitFor(() => expect(service.runs.mock.calls.length).toBe(before + 1));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps the run when the owner changes their mind', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS) });
    await renderHistory({ service });
    await screen.findByRole('button', { name: 'Open run 30' });

    press('Delete run 30');
    press('Keep');

    expect(service.deleteRun).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Delete run 30' })).toBeTruthy();
  });

  it('shows the server’s reason when it will not delete a run that is going, and still reads the list again', async () => {
    const service = fakeService(vi, {
      runs: vi.fn(async () => RUNS),
      deleteRun: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RUN_IS_RUNNING', 'That run is still going. Cancel it first.', 409);
      }),
    });
    await renderHistory({ service });
    await screen.findByRole('button', { name: 'Open run 31' });
    const before = service.runs.mock.calls.length;

    press('Delete run 31');
    press('Confirm delete');

    expect((await screen.findByText('That run is still going. Cancel it first.')).getAttribute('role')).toBe('alert');
    await waitFor(() => expect(service.runs.mock.calls.length).toBe(before + 1));
  });

  it('says plainly when a delete could not be made at all', async () => {
    const service = fakeService(vi, {
      runs: vi.fn(async () => RUNS),
      deleteRun: vi.fn(async () => {
        throw new Error('offline');
      }),
    });
    await renderHistory({ service });
    await screen.findByRole('button', { name: 'Open run 30' });

    press('Delete run 30');
    press('Confirm delete');

    expect(await screen.findByText('The run could not be deleted. Try again.')).toBeTruthy();
  });

  it('forgets an earlier refusal when the next delete is asked for', async () => {
    let refuse = true;
    const service = fakeService(vi, {
      runs: vi.fn(async () => RUNS),
      deleteRun: vi.fn(async () => {
        if (refuse) throw new RunRefusedError('X', 'Not now.', 409);
      }),
    });
    await renderHistory({ service });
    await screen.findByRole('button', { name: 'Open run 31' });
    press('Delete run 31');
    press('Confirm delete');
    await screen.findByText('Not now.');

    refuse = false;
    press('Delete run 30');
    press('Confirm delete');

    await waitFor(() => expect(screen.queryByText('Not now.')).toBeNull());
  });
});

describe('deleting a run, and the keyboard', () => {
  const without = (id: number) => RUNS.filter((run) => run.id !== id);

  async function deleteWithTheKeyboard(id: number, listAfter: RunListItem[]) {
    const reads = [RUNS, listAfter];
    const service = fakeService(vi, { runs: vi.fn(async () => reads.shift() ?? listAfter) });
    await renderHistory({ service });
    await screen.findByRole('button', { name: `Open run ${id}` });
    press(`Delete run ${id}`);
    screen.getByRole<HTMLButtonElement>('button', { name: 'Confirm delete' }).focus();
    press('Confirm delete');
    return service;
  }

  it.each([
    ['goes to the Open button of the run that now stands where the deleted one stood', 30, 'Open run 29'],
    ['goes to the last run when the one deleted was the last', 27, 'Open run 28'],
  ])('%s', async (_name, deleted, opens) => {
    await deleteWithTheKeyboard(deleted, without(deleted));

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: opens })));
  });

  it('goes to the heading of the list when no run is left', async () => {
    const service = fakeService(vi, { runs: vi.fn().mockResolvedValueOnce([COMPLETE]).mockResolvedValue([]) });
    await renderHistory({ service });
    await screen.findByRole('button', { name: 'Open run 30' });
    press('Delete run 30');
    screen.getByRole<HTMLButtonElement>('button', { name: 'Confirm delete' }).focus();

    press('Confirm delete');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Runs' })));
  });

  it('goes to the Open button of the same run when the server would not delete it', async () => {
    const service = fakeService(vi, {
      runs: vi.fn(async () => RUNS),
      deleteRun: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RUN_IS_RUNNING', 'That run is still going. Cancel it first.', 409);
      }),
    });
    await renderHistory({ service });
    await screen.findByRole('button', { name: 'Open run 31' });
    press('Delete run 31');
    screen.getByRole<HTMLButtonElement>('button', { name: 'Confirm delete' }).focus();

    press('Confirm delete');

    await screen.findByText('That run is still going. Cancel it first.');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open run 31' })));
  });

  it('tells the page which run was deleted, and tells it of none that was refused', async () => {
    const refused = new RunRefusedError('OPTIONS_BACKTEST_RUN_IS_RUNNING', 'That run is still going.', 409);
    const service = fakeService(vi, {
      runs: vi.fn(async () => RUNS),
      deleteRun: vi.fn(async (id: number) => {
        if (id === 31) throw refused;
      }),
    });
    const { view } = await renderHistory({ service });
    const deleted: number[] = [];
    view.fixture.componentInstance.deleted.subscribe((id) => deleted.push(id));
    await screen.findByRole('button', { name: 'Open run 31' });

    press('Delete run 31');
    press('Confirm delete');
    await screen.findByText('That run is still going.');
    press('Delete run 30');
    press('Confirm delete');

    await waitFor(() => expect(deleted).toEqual([30]));
  });
});
