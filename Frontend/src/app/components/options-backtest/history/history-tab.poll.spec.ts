import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderHistoryTab } from '../testing/render-history';
import { fakeService, type FakeService } from '../testing/render-page';
import { runDetail, runItem } from '../testing/run-fixtures';
import { reads } from '../testing/page-actions';
import type { RunListItem } from '../options-backtest.types';

/**
 * While a run in the list is going the list is read again every few seconds, so that a run that ends shows as ended and
 * can be deleted without leaving the tab. Under fake timers that also move with the clock, so no spec asks for a read
 * at the very edge of an interval.
 */
const POLL_MS = 3_000;

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

const after = (ms: number) => vi.advanceTimersByTimeAsync(ms);
const GOING = runItem({ id: 31, job_id: 'job-31', status: 'running' });
const OVER = runItem({ id: 31, job_id: 'job-31', status: 'complete', finished_at_ms: 1, headline: { sessions_tested: 5, sessions_traded: 4, trades: 6, trades_won: 3, net_profit_usd: '88.10', ending_cash_usd: '10088.10' } });
const OTHER = runItem({ id: 30, job_id: 'job-30', status: 'complete', finished_at_ms: 1 });
const statusOf = (id: number) => reads((screen.getByRole('button', { name: `Open run ${id}` }).closest('tr') as HTMLElement).querySelectorAll('td')[1] ?? null);

function serving(...lists: RunListItem[][]): FakeService {
  let next = 0;
  return fakeService(vi, { runs: vi.fn(async () => lists[Math.min(next++, lists.length - 1)] ?? []) });
}

describe('the list, while a run in it is going', () => {
  it('is read again every few seconds, so that the run shows as ended with its figures and can be deleted, and then it is read no more', async () => {
    const service = serving([GOING, OTHER], [GOING, OTHER], [OVER, OTHER]);
    await renderHistoryTab(vi, { service, polling: true });
    await screen.findByRole('button', { name: 'Open run 31' });
    expect(statusOf(31)).toBe('Running');
    expect(service.runs).toHaveBeenCalledTimes(1);

    await after(1_000);
    expect(service.runs).toHaveBeenCalledTimes(1);
    await after(POLL_MS);
    await waitFor(() => expect(service.runs).toHaveBeenCalledTimes(2));
    expect(statusOf(31)).toBe('Running');
    await after(POLL_MS);

    await waitFor(() => expect(statusOf(31)).toBe('Complete'));
    expect(reads((screen.getByRole('button', { name: 'Open run 31' }).closest('tr') as HTMLElement).querySelectorAll('td')[4] ?? null)).toContain('$88.10');
    fireEvent.click(screen.getByRole('button', { name: 'Delete run 31' }));
    expect(screen.getByRole('button', { name: 'Confirm delete' })).toBeTruthy();
    await after(10 * POLL_MS);
    expect(service.runs).toHaveBeenCalledTimes(3);
  });

  it('is not read again when no run in it is going', async () => {
    const service = serving([OTHER]);
    await renderHistoryTab(vi, { service, polling: true });
    await screen.findByRole('button', { name: 'Open run 30' });

    await after(5 * POLL_MS);

    expect(service.runs).toHaveBeenCalledTimes(1);
  });

  it('goes on being read when one read fails, and says so', async () => {
    const service = fakeService(vi);
    service.runs
      .mockResolvedValueOnce([GOING])
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue([OVER]);
    await renderHistoryTab(vi, { service, polling: true });
    await screen.findByRole('button', { name: 'Open run 31' });

    await after(POLL_MS);
    expect(await screen.findByText('The runs could not be read.')).toBeTruthy();
    await after(POLL_MS);

    await waitFor(() => expect(statusOf(31)).toBe('Complete'));
    expect(screen.queryByText('The runs could not be read.')).toBeNull();
  });

  it('is not read while the tab is not the one on show, or while a run is open over it, and is read again when it is back', async () => {
    const service = serving([GOING], [GOING]);
    service.run.mockResolvedValue(runDetail({ ...GOING, status: 'running', figures: null, slippage: [], slippage_tolerance: null, headline: null }));
    const { view } = await renderHistoryTab(vi, { service, polling: true });
    await screen.findByRole('button', { name: 'Open run 31' });
    expect(service.runs).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Open run 31' }));
    await screen.findByRole('region', { name: 'Backtest run' });
    const whileOpen = service.runs.mock.calls.length;
    await after(3 * POLL_MS);
    expect(service.runs.mock.calls.length).toBe(whileOpen);
    fireEvent.click(screen.getByRole('button', { name: 'Back to the runs' }));
    await waitFor(() => expect(service.runs.mock.calls.length).toBe(whileOpen + 1));
    await after(POLL_MS);
    await waitFor(() => expect(service.runs.mock.calls.length).toBe(whileOpen + 2));

    view.fixture.componentRef.setInput('active', false);
    await after(10);
    const hidden = service.runs.mock.calls.length;
    await after(3 * POLL_MS);
    expect(service.runs.mock.calls.length).toBe(hidden);
  });

  it('stops being read when the tab is gone', async () => {
    const service = serving([GOING]);
    const { view } = await renderHistoryTab(vi, { service, polling: true });
    await screen.findByRole('button', { name: 'Open run 31' });

    view.fixture.destroy();
    await after(5 * POLL_MS);

    expect(service.runs).toHaveBeenCalledTimes(1);
  });
});
