import { screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { RunQuery } from '../options-backtest.service';
import type { RunListItem } from '../options-backtest.types';
import { deferred } from '../past-chain/testing/deferred';
import { renderHistoryTab } from '../testing/render-history';
import { press } from '../testing/page-actions';
import { fakeService } from '../testing/render-page';
import { runItem } from '../testing/run-fixtures';
import { RUNS_LISTED } from './runs-listed';

/** A server that holds `held` runs, newest first (ids `held` down to 1), and lists the first `limit` of them as its route does. */
function serverHolding(held: number) {
  const all = Array.from({ length: held }, (_, at) => runItem({ id: held - at, job_id: `job-${held - at}`, status: 'complete' }));
  return vi.fn(async (query?: RunQuery): Promise<RunListItem[]> => all.slice(0, query?.limit ?? 200));
}

/** Two runs to a page and at most six, so that a spec draws a handful of rows and not a thousand. */
const SMALL = { provide: RUNS_LISTED, useValue: { page: 2, most: 6 } };
const olderButton = () => screen.queryByRole('button', { name: 'Show older runs' });
const openButtons = () => screen.getAllByRole('button', { name: /^Open run / }).map((button) => button.getAttribute('aria-label'));

describe('a list the server may have cut short', () => {
  it('asks for the newest 200 first, and says nothing and offers nothing when the server held fewer', async () => {
    const service = fakeService(vi, { runs: serverHolding(3) });
    await renderHistoryTab(vi, { service });
    await screen.findByRole('button', { name: 'Open run 3' });

    expect(service.runs).toHaveBeenLastCalledWith({ limit: 200 });
    expect(olderButton()).toBeNull();
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
  });

  it('says there may be older runs when the server listed as many as were asked, and shows more on request until none are left out', async () => {
    const service = fakeService(vi, { runs: serverHolding(5) });
    await renderHistoryTab(vi, { service, providers: [SMALL] });
    await screen.findByRole('button', { name: 'Open run 5' });

    expect(openButtons()).toEqual(['Open run 5', 'Open run 4']);
    expect(screen.getByText('Showing the newest 2 runs. There may be older ones.')).toBeTruthy();

    press('Show older runs');
    await screen.findByRole('button', { name: 'Open run 2' });

    expect(service.runs).toHaveBeenLastCalledWith({ limit: 4 });
    expect(openButtons()).toEqual(['Open run 5', 'Open run 4', 'Open run 3', 'Open run 2']);
    expect(screen.getByText('Showing the newest 4 runs. There may be older ones.')).toBeTruthy();

    press('Show older runs');
    await screen.findByRole('button', { name: 'Open run 1' });

    expect(service.runs).toHaveBeenLastCalledWith({ limit: 6 });
    expect(openButtons()).toHaveLength(5);
    expect(olderButton()).toBeNull();
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
  });

  it('stops at the most the server lists, says so, and tells the owner how to bring older runs into view', async () => {
    const service = fakeService(vi, { runs: serverHolding(9) });
    await renderHistoryTab(vi, { service, providers: [{ provide: RUNS_LISTED, useValue: { page: 2, most: 4 } }] });
    await screen.findByRole('button', { name: 'Open run 9' });

    press('Show older runs');
    await screen.findByRole('button', { name: 'Open run 6' });

    expect(service.runs).toHaveBeenLastCalledWith({ limit: 4 });
    expect(olderButton()).toBeNull();
    expect(screen.getByText('Showing the newest 4 runs, the most the server lists. Delete runs here to bring older ones into view.')).toBeTruthy();
  });

  it('keeps the runs on screen while the older ones are being read', async () => {
    const more = deferred<RunListItem[]>();
    const all = serverHolding(5);
    const service = fakeService(vi, { runs: vi.fn((query?: RunQuery) => (query?.limit === 4 ? more.promise : all(query))) });
    await renderHistoryTab(vi, { service, providers: [SMALL] });
    await screen.findByRole('button', { name: 'Open run 5' });

    press('Show older runs');
    await waitFor(() => expect(service.runs).toHaveBeenLastCalledWith({ limit: 4 }));

    expect(openButtons()).toEqual(['Open run 5', 'Open run 4']);
    expect(screen.queryByText('Loading…')).toBeNull();
    more.resolve(await all({ limit: 4 }));
    await screen.findByRole('button', { name: 'Open run 2' });
  });

  it('reads as many runs again after a delete as are on screen, and does not fall back to the first page', async () => {
    const service = fakeService(vi, { runs: serverHolding(5) });
    await renderHistoryTab(vi, { service, providers: [SMALL] });
    await screen.findByRole('button', { name: 'Open run 5' });
    press('Show older runs');
    await screen.findByRole('button', { name: 'Open run 2' });

    press('Delete run 5');
    press('Confirm delete');

    await waitFor(() => expect(service.deleteRun).toHaveBeenCalledWith(5));
    await waitFor(() => expect(service.runs.mock.calls.filter(([query]) => query?.limit === 4).length).toBeGreaterThanOrEqual(2));
    expect(service.runs).toHaveBeenLastCalledWith({ limit: 4 });
  });
});
