import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { comparison } from '../run-detail/testing/detail-fixtures';
import { fakeRunDetail } from '../run-detail/testing/fake-run-detail';
import { renderHistoryTab } from '../testing/render-history';
import { fakeService } from '../testing/render-page';
import { runItem } from '../testing/run-fixtures';

/**
 * While a run in the list is going the list is read again every few seconds; a comparison on show takes the place of the
 * list, so the list is not read behind it, and is read again once the owner is back. Under fake timers that also move with
 * the clock, so no spec asks for a read at the very edge of an interval.
 */
const POLL_MS = 3_000;

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

const after = (ms: number) => vi.advanceTimersByTimeAsync(ms);
const HEADLINE = { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: '1234.56', ending_cash_usd: '11234.56' };
const RUNS = [
  runItem({ id: 33, job_id: 'job-33', status: 'complete', headline: HEADLINE, finished_at_ms: 1 }),
  runItem({ id: 32, job_id: 'job-32', status: 'running' }),
  runItem({ id: 30, job_id: 'job-30', status: 'complete', headline: HEADLINE, finished_at_ms: 1 }),
];

describe('the list, while a comparison is on show', () => {
  it('is not read behind the comparison though a run in it is going, and is read again once the owner is back', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => RUNS) });
    await renderHistoryTab(vi, { service, polling: true, detail: fakeRunDetail(vi, { compare: vi.fn(async () => comparison()) }) });
    await screen.findByRole('button', { name: 'Open run 33' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Compare run 30' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Compare run 33' }));
    fireEvent.click(screen.getByRole('button', { name: 'Compare' }));
    await screen.findByRole('region', { name: 'Sessions both runs tested' });
    const whileComparing = service.runs.mock.calls.length;

    await after(3 * POLL_MS);
    expect(service.runs.mock.calls.length).toBe(whileComparing);
    fireEvent.click(screen.getByRole('button', { name: 'Back to the runs' }));

    await waitFor(() => expect(service.runs.mock.calls.length).toBe(whileComparing + 1));
    await after(POLL_MS);
    await waitFor(() => expect(service.runs.mock.calls.length).toBe(whileComparing + 2));
  });
});
