import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { OptionsBacktestService, RunRefusedError } from '../options-backtest.service';
import type { RunListItem } from '../options-backtest.types';
import { fakeJobs, type FakeJobs } from '../testing/fake-jobs';
import { expectTradingDatesInNewYork, lettingGo, press, reads } from '../testing/page-actions';
import { fakeService, type FakeService } from '../testing/render-page';
import { runDetail, runItem } from '../testing/run-fixtures';
import { sessionOpenMs } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { HistoryTabComponent } from './history-tab.component';

const HEADLINE = { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: '1234.56', ending_cash_usd: '11234.56' };

const COMPLETE = runItem({ id: 30, job_id: 'job-30', status: 'complete', setup_name: 'Straddle', underlying: 'AAPL', headline: HEADLINE, finished_at_ms: 1 });
const RUNNING = runItem({ id: 31, job_id: 'job-31', status: 'running' });
const FAILED = runItem({ id: 29, job_id: 'job-29', status: 'failed', failure: { code: 'DATA_MISSING', reason: 'Some sessions have no data.', items: [{ session_open_ms: sessionOpenMs('2026-09-15'), code: 'listing', message: 'No listing was stored.' }] } });
const CANCELLED = runItem({ id: 28, job_id: 'job-28', status: 'cancelled' });
const INTERRUPTED = runItem({ id: 27, job_id: 'job-27', status: 'interrupted' });
const RUNS = [RUNNING, COMPLETE, FAILED, CANCELLED, INTERRUPTED];

async function renderHistory(options: { service?: FakeService; jobs?: FakeJobs; active?: boolean } = {}) {
  const service = options.service ?? fakeService(vi, { runs: vi.fn(async () => RUNS) });
  const jobs = options.jobs ?? fakeJobs();
  const view = await render(HistoryTabComponent, {
    inputs: { active: options.active ?? true },
    providers: [...fakePickerWorld().providers, { provide: OptionsBacktestService, useValue: service }, { provide: JobsService, useValue: jobs.service }],
  });
  return { view, service, jobs };
}

const rowFor = (id: number) => screen.getByRole('button', { name: `Open run ${id}` }).closest('tr') as HTMLElement;

describe('the list of runs', () => {
  it('lists the runs in the order the server gave, newest first', async () => {
    await renderHistory();

    await screen.findAllByRole('row');

    const ids = screen.getAllByRole('button', { name: /^Open run / }).map((button) => button.getAttribute('aria-label'));
    expect(ids).toEqual(['Open run 31', 'Open run 30', 'Open run 29', 'Open run 28', 'Open run 27']);
  });

  it('says when each was started, how it ended, the setup’s name and underlying, and the dates in New York dates', async () => {
    await renderHistory();
    await screen.findAllByRole('row');

    const row = rowFor(30);

    const cells = [...row.querySelectorAll('td')].map((cell) => reads(cell));
    expect(cells[0]).toMatch(/^2026-09-2[12] \d\d:\d\d:\d\d$/);
    expect(cells.slice(1, 4)).toEqual(['Complete', 'AAPL Straddle', '2026-09-14 – 2026-09-18']);
    expectTradingDatesInNewYork(row.querySelectorAll('td')[3]);
    expect(reads(rowFor(31).querySelectorAll('td')[2])).toBe('SPY');
  });

  it('shows the headline figures of a complete run as the server wrote them, and none for a run that is not', async () => {
    await renderHistory();
    await screen.findAllByRole('row');

    expect(reads(rowFor(30).querySelectorAll('td')[4])).toBe('Net profit $1,234.56 Ending cash $11,234.56 Trades 12, won 7 Sessions traded 8 of 10');
    for (const id of [31, 29, 28, 27]) expect(reads(rowFor(id).querySelectorAll('td')[4])).toBe('');
  });

  it('names every status in words', async () => {
    await renderHistory();
    await screen.findAllByRole('row');

    expect([31, 30, 29, 28, 27].map((id) => reads(rowFor(id).querySelectorAll('td')[1]))).toEqual(['Running', 'Complete', 'Failed', 'Cancelled', 'Interrupted']);
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
    expect(within(panel).getByText('2 of 5 sessions')).toBeTruthy();
    expect(within(panel).getByRole('button', { name: 'Cancel' })).toBeTruthy();
    expect(service.run).toHaveBeenCalledWith(31);
  });

  it('shows a complete run’s banner and result, and the setup it used in the server’s sentences', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...COMPLETE })) });
    await renderHistory({ service });

    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));

    expect(await screen.findByRole('note', { name: 'How these figures were made' })).toBeTruthy();
    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(await screen.findByText('Window for SPY.')).toBeTruthy();
    expect(service.describe).toHaveBeenCalledWith(runDetail().setup);
  });

  it('shows why a failed run failed, and each item with its session’s date', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...FAILED, status: 'failed', figures: null, slippage: [], slippage_tolerance: null, headline: null })) });
    await renderHistory({ service });

    fireEvent.click(await screen.findByRole('button', { name: 'Open run 29' }));

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Some sessions have no data.')).toBeTruthy();
    expect(reads(within(alert).getByRole('listitem'))).toBe('2026-09-15 – No listing was stored.');
    expectTradingDatesInNewYork(alert);
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

  it('goes back to the list, read again', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...COMPLETE })) });
    await renderHistory({ service });
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByRole('region', { name: 'Headline figures' });
    const before = service.runs.mock.calls.length;

    press('Back to the runs');

    await screen.findByRole('button', { name: 'Open run 30' });
    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
    expect(service.runs.mock.calls.length).toBe(before + 1);
  });

  it('puts the keyboard back on the Open button of the run, when the owner goes back to the list', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...COMPLETE })) });
    await renderHistory({ service });
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    await screen.findByRole('region', { name: 'Headline figures' });

    press('Back to the runs');

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open run 30' })));
  });

  it('puts the keyboard on the run it opened', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS), run: vi.fn(async () => runDetail({ ...COMPLETE })) });
    await renderHistory({ service });

    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Backtest run' })));
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
