import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { RunDetailRefusedError } from '../run-detail/run-detail.service';
import { comparison } from '../run-detail/testing/detail-fixtures';
import { fakeRunDetail, type FakeRunDetail } from '../run-detail/testing/fake-run-detail';
import type { RunListItem } from '../options-backtest.types';
import { reads } from '../testing/page-actions';
import { renderHistoryTab } from '../testing/render-history';
import { fakeService, type FakeService } from '../testing/render-page';
import { runItem } from '../testing/run-fixtures';

const HEADLINE = { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: '1234.56', ending_cash_usd: '11234.56' };
const complete = (id: number) => runItem({ id, job_id: `job-${id}`, status: 'complete', headline: HEADLINE, finished_at_ms: 1 });
const RUNS: RunListItem[] = [
  complete(34),
  runItem({ id: 33, job_id: 'job-33', status: 'complete', headline: HEADLINE, finished_at_ms: 1 }),
  runItem({ id: 32, job_id: 'job-32', status: 'running' }),
  complete(30),
  runItem({ id: 29, job_id: 'job-29', status: 'failed', failure: { code: 'X', reason: 'No data.', items: [] } }),
];

async function renderHistory(options: { service?: FakeService; detail?: FakeRunDetail } = {}) {
  const service = options.service ?? fakeService(vi, { runs: vi.fn(async () => RUNS) });
  const detail = options.detail ?? fakeRunDetail(vi, { compare: vi.fn(async () => comparison()) });
  const rendered = await renderHistoryTab(vi, { service, detail });
  await screen.findAllByRole('row');
  return { ...rendered, service, detail };
}

const box = (id: number) => screen.getByRole<HTMLInputElement>('checkbox', { name: `Compare run ${id}` });
const tick = (...ids: number[]) => ids.forEach((id) => fireEvent.click(box(id)));
const compareButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Compare' });
const hint = () => reads(document.querySelector('.hint'));

describe('the history, choosing two runs to compare', () => {
  it('has a column for it and a box for each run, named by the run, keeps its other columns where they were, and describes each box by the same sentence', async () => {
    await renderHistory();

    const row = screen.getByRole('button', { name: 'Open run 30' }).closest('tr') as HTMLElement;
    const hintId = document.querySelector('.hint')?.id ?? '';

    expect.soft(screen.getByRole('columnheader', { name: 'Compare' }), 'has a column for it').toBeTruthy();
    expect.soft(screen.getAllByRole('checkbox').map((each) => each.getAttribute('aria-label')), 'has a box for each run, named by the run').toEqual(['Compare run 34', 'Compare run 33', 'Compare run 32', 'Compare run 30', 'Compare run 29']);
    expect.soft([...row.querySelectorAll('td')].length, 'keeps its other columns where they were (count)').toBe(7);
    expect.soft(reads(row.querySelectorAll('td')[1]), 'keeps its other columns where they were, so the cells read as they did (status)').toBe('Complete');
    expect.soft(hintId, 'describes each box by the same sentence (the hint has an id)').not.toBe('');
    expect.soft(screen.getAllByRole('checkbox').every((each) => each.getAttribute('aria-describedby') === hintId), 'describes each box by the same sentence, so a disabled box has its reason').toBe(true);
    expect.soft(compareButton().getAttribute('aria-describedby'), 'describes the Compare button by the same sentence').toBe(hintId);
  });

  it('lets only a complete run be ticked: a running one and a failed one have a box that is disabled', async () => {
    await renderHistory();

    expect([34, 33, 32, 30, 29].map((id) => box(id).disabled)).toEqual([false, false, true, false, true]);
    await userEvent.click(box(32));
    await userEvent.click(box(29));
    expect([32, 29].map((id) => box(id).checked)).toEqual([false, false]);
    expect(hint()).toBe('Tick two complete runs to compare them.');
  });

  it('offers Compare only when exactly two runs are ticked, and says what to do at each step', async () => {
    await renderHistory();

    expect([compareButton().disabled, hint()]).toEqual([true, 'Tick two complete runs to compare them.']);
    tick(30);
    expect([compareButton().disabled, hint()]).toEqual([true, 'Tick one more complete run.']);
    tick(33);
    expect([compareButton().disabled, hint()]).toEqual([false, 'Run 30 and run 33 are ticked.']);
  });

  it('unticks nothing when a third is ticked: it says what to do, and Compare stays off until two are left', async () => {
    await renderHistory();
    tick(30, 33, 34);

    expect([30, 33, 34].map((id) => box(id).checked)).toEqual([true, true, true]);
    expect([compareButton().disabled, hint()]).toEqual([true, 'More than two runs are ticked. Untick until two are left.']);
    fireEvent.click(box(33));
    expect([30, 33, 34].map((id) => box(id).checked)).toEqual([true, false, true]);
    expect([compareButton().disabled, hint()]).toEqual([false, 'Run 30 and run 34 are ticked.']);
  });

  it('leaves a run out of the two when it leaves the list, and says one more is needed', async () => {
    let listed = RUNS;
    const service = fakeService(vi, { runs: vi.fn(async () => listed) });
    await renderHistory({ service });
    tick(30, 33);
    expect(compareButton().disabled).toBe(false);

    listed = RUNS.filter((run) => run.id !== 33);
    fireEvent.click(within(screen.getByRole('button', { name: 'Open run 33' }).closest('tr') as HTMLElement).getByRole('button', { name: 'Delete run 33' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm delete' }));

    await waitFor(() => expect(screen.queryByRole('checkbox', { name: 'Compare run 33' })).toBeNull());
    expect(box(30).checked).toBe(true);
    expect([compareButton().disabled, hint()]).toEqual([true, 'Tick one more complete run.']);
  });
});

describe('the history, comparing', () => {
  it('asks for the two runs in the order they were ticked, whatever their order in the list', async () => {
    const { detail } = await renderHistory();
    tick(33, 30);

    fireEvent.click(compareButton());

    await screen.findByRole('heading', { name: 'Run 33 compared with run 30' });
    expect(detail.compare).toHaveBeenCalledWith(33, 30);
  });

  it('shows the comparison in the place of the list, with a way back, and moves the keyboard to its heading', async () => {
    await renderHistory();
    tick(30, 33);

    fireEvent.click(compareButton());

    await screen.findByRole('region', { name: 'Sessions both runs tested' });
    expect(screen.queryByRole('button', { name: 'Open run 30' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Runs' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Back to the runs' })).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Run 30 compared with run 33' })));
  });

  it('comes back to the list on Back, with the keyboard on the Compare button, and the two runs still ticked', async () => {
    await renderHistory();
    tick(30, 33);
    fireEvent.click(compareButton());
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    fireEvent.click(screen.getByRole('button', { name: 'Back to the runs' }));

    await screen.findByRole('heading', { name: 'Runs' });
    await waitFor(() => expect(document.activeElement).toBe(compareButton()));
    expect([30, 33].map((id) => box(id).checked)).toEqual([true, true]);
    expect(screen.queryByRole('region', { name: 'Sessions both runs tested' })).toBeNull();
  });

  it('shows the server’s refusal in its words, with a way back', async () => {
    const message = 'Only complete runs can be compared, and run 33 (b) is running.';
    const detail = fakeRunDetail(vi, { compare: vi.fn(async () => { throw new RunDetailRefusedError('OPTIONS_BACKTEST_RUN_NOT_COMPLETE', message, 409); }) });
    await renderHistory({ detail });
    tick(30, 33);

    fireEvent.click(compareButton());

    expect(reads(await screen.findByRole('alert'))).toBe(message);
    fireEvent.click(screen.getByRole('button', { name: 'Back to the runs' }));
    await screen.findByRole('heading', { name: 'Runs' });
  });

  it('can compare again after coming back, with a different pair', async () => {
    const { detail } = await renderHistory();
    tick(30, 33);
    fireEvent.click(compareButton());
    await screen.findByRole('region', { name: 'Sessions both runs tested' });
    fireEvent.click(screen.getByRole('button', { name: 'Back to the runs' }));
    await screen.findByRole('heading', { name: 'Runs' });

    fireEvent.click(box(33));
    tick(34);
    fireEvent.click(compareButton());

    await screen.findByRole('heading', { name: 'Run 30 compared with run 34' });
    expect(detail.compare).toHaveBeenLastCalledWith(30, 34);
  });
});

describe('the history, for a screen reader', () => {
  async function violations(): Promise<string[]> {
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    return results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`);
  }

  it('has no violation with the Compare column, two runs ticked and a third', async () => {
    await renderHistory();
    tick(30, 33, 34);

    expect(await violations()).toEqual([]);
  });

  it('has no violation with a comparison showing', async () => {
    await renderHistory();
    tick(30, 33);
    fireEvent.click(compareButton());
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    expect(await violations()).toEqual([]);
  });
});
