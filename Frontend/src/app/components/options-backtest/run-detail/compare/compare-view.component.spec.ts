import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { sessionOpenMs } from '../../../../shared/date-picker/testing/fake-trading-calendar';
import { lettingGo, reads } from '../../testing/page-actions';
import { RunDetailRefusedError, RunDetailService } from '../run-detail.service';
import type { RunComparison } from '../run-detail.types';
import { comparedRun, comparison } from '../testing/detail-fixtures';
import { fakeRunDetail, type FakeRunDetail } from '../testing/fake-run-detail';
import { CompareViewComponent } from './compare-view.component';

async function renderCompare(detail: FakeRunDetail, a = 30, b = 33) {
  return render(CompareViewComponent, { inputs: { a, b }, providers: [{ provide: RunDetailService, useValue: detail }] });
}

const serving = (answer: RunComparison = comparison()) => fakeRunDetail(vi, { compare: vi.fn(async () => answer) });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const settingsRows = () => within(screen.getByRole('region', { name: 'Settings that differ (scrolls sideways)' })).getAllByRole('row').slice(1).map((row) => [...row.querySelectorAll('th, td')].map((cell) => reads(cell)));
const totalsRows = () => within(screen.getByRole('region', { name: 'Totals over the shared sessions (scrolls sideways)' })).getAllByRole('row').slice(1).map((row) => [...row.querySelectorAll('th, td')].map((cell) => reads(cell)));
const sessionRows = () => within(screen.getByRole('region', { name: 'Each shared session (scrolls sideways)' })).getAllByRole('row').slice(1).map((row) => [...row.querySelectorAll('th, td')].map((cell) => reads(cell)));

describe('comparing two runs, reading them', () => {
  it('asks for the two runs by their ids, in the order given, under a heading that names them', async () => {
    const detail = serving();
    await renderCompare(detail, 33, 30);

    await screen.findByRole('heading', { name: 'What differs between the two runs' });
    expect(detail.compare).toHaveBeenCalledTimes(1);
    expect(detail.compare).toHaveBeenCalledWith(33, 30);
    expect(screen.getByRole('heading', { name: 'Run 33 compared with run 30' })).toBeTruthy();
  });

  it('says it is reading while the answer is on its way', async () => {
    const waiting = deferred<RunComparison>();
    await renderCompare(fakeRunDetail(vi, { compare: vi.fn(() => waiting.promise) }));

    expect((await screen.findByText('Reading the comparison…')).getAttribute('role')).toBe('status');
    expect(screen.queryByRole('region', { name: 'Sessions both runs tested' })).toBeNull();
  });

  it('names the two runs, which is run A and which run B, the underlying, and when each was started', async () => {
    await renderCompare(serving());
    await screen.findByRole('list', { name: 'The two runs' });

    const items = within(screen.getByRole('list', { name: 'The two runs' })).getAllByRole('listitem');
    expect(items.map((item) => reads(item))).toEqual([
      expect.stringMatching(/^Run A is run 30 , Straddle , of SPY , started 2026-09-2[12] \d\d:\d\d:\d\d$/),
      expect.stringMatching(/^Run B is run 33 , of SPY , started 2026-09-2[23] \d\d:\d\d:\d\d$/),
    ]);
  });
});

describe('comparing two runs, what differs comes first', () => {
  it('comes before the shared sessions and before any figure on the page', async () => {
    const { container } = await renderCompare(serving());
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    const differs = container.querySelector('app-options-backtest-compare-settings');
    const shared = container.querySelector('app-options-backtest-compare-sessions');
    expect(differs?.compareDocumentPosition(shared as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(reads(differs).includes('$')).toBe(false);
  });

  it('lists every setting that differs as the server worded it: the setting, then the value in run A and in run B', async () => {
    await renderCompare(serving());
    await screen.findByRole('region', { name: 'Settings that differ (scrolls sideways)' });

    expect(settingsRows()).toEqual([
      ['leg call: re-entry on stop', 'not set', 'at once, up to 1 time'],
      ['slippage', '0.3%', '0.45%'],
      ['fees', 'Fees are not charged.', 'Fees were set by the owner at 0.65 a contract.'],
    ]);
  });

  it('keeps the order the server gave and shows a setting only the server named', async () => {
    const answer = comparison({ setting_differences: [{ setting: 'leg', leg_id: 'put', label: 'leg put', a: 'not in this run', b: 'a put, 1 contract, expiring this week' }] });
    await renderCompare(serving(answer));
    await screen.findByRole('region', { name: 'Settings that differ (scrolls sideways)' });

    expect(settingsRows()).toEqual([['leg put', 'not in this run', 'a put, 1 contract, expiring this week']]);
  });

  it('says the two runs used the same settings and the same dates when nothing differs', async () => {
    await renderCompare(serving(comparison({ setting_differences: [], range_difference: null })));

    expect((await screen.findByText('The two runs used the same settings and the same dates.')).getAttribute('role')).toBe('status');
    expect(screen.queryByRole('region', { name: 'Settings that differ (scrolls sideways)' })).toBeNull();
  });

  it('shows the dates of each run, in New York dates, when the ranges differ', async () => {
    const range = {
      a: { first_session_open_ms: sessionOpenMs('2026-09-14'), last_session_open_ms: sessionOpenMs('2026-09-18'), sessions_requested: 5 },
      b: { first_session_open_ms: sessionOpenMs('2026-09-15'), last_session_open_ms: sessionOpenMs('2026-09-21'), sessions_requested: 5 },
    };
    await renderCompare(serving(comparison({ range_difference: range })));
    const dates = await screen.findByRole('region', { name: 'Dates that differ (scrolls sideways)' });

    expect([...within(dates).getAllByRole('row')].slice(1).map((row) => [...row.querySelectorAll('th, td')].map((cell) => reads(cell)))).toEqual([
      ['First day', '2026-09-14', '2026-09-15'],
      ['Last day', '2026-09-18', '2026-09-21'],
      ['Sessions asked for', '5', '5'],
    ]);
    expect(dates.querySelectorAll('app-timestamp-display[data-timestamp-mode="date-et"]').length).toBe(4);
  });

  it('says the settings are the same, but still shows the dates, when only the dates differ', async () => {
    const range = {
      a: { first_session_open_ms: sessionOpenMs('2026-09-14'), last_session_open_ms: sessionOpenMs('2026-09-18'), sessions_requested: 5 },
      b: { first_session_open_ms: sessionOpenMs('2026-09-14'), last_session_open_ms: sessionOpenMs('2026-09-25'), sessions_requested: 10 },
    };
    await renderCompare(serving(comparison({ setting_differences: [], range_difference: range })));

    expect(await screen.findByText('The settings are the same in both runs.')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Dates that differ (scrolls sideways)' })).toBeTruthy();
    expect(screen.queryByText('The two runs used the same settings and the same dates.')).toBeNull();
  });
});

describe('comparing two runs, the sessions they share', () => {
  it('counts the shared sessions and each run’s own, from the served numbers', async () => {
    await renderCompare(serving());
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    expect(reads(screen.getByRole('region', { name: 'Sessions both runs tested' }).querySelector('p'))).toBe(
      'Sessions in both runs: 3. Only in run A: 1. Only in run B: 0. The figures below are for the shared sessions only.',
    );
  });

  it('says one shared session with a label and a number, which needs no plural', async () => {
    const answer = comparison();
    await renderCompare(serving({ ...answer, overlap: { ...answer.overlap, shared_sessions: 1, sessions: answer.overlap.sessions.slice(0, 1) } }));
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    expect(reads(screen.getByRole('region', { name: 'Sessions both runs tested' }).querySelector('p'))).toContain('Sessions in both runs: 1.');
  });

  it('shows each run’s net profit, trades and winning sessions over the shared sessions as the server worked them out', async () => {
    await renderCompare(serving());
    await screen.findByRole('region', { name: 'Totals over the shared sessions (scrolls sideways)' });

    expect(totalsRows()).toEqual([
      ['Net profit', '$120.50', '-$70.25'],
      ['Trades', '6', '7'],
      ['Winning sessions', '2', '1'],
    ]);
  });

  it('shows the pair of profits of each shared session, with each run’s status, in New York dates', async () => {
    const { container } = await renderCompare(serving());
    await screen.findByRole('region', { name: 'Each shared session (scrolls sideways)' });

    expect(sessionRows()).toEqual([
      ['2026-09-14', 'Traded', '$150.25', 'Traded', '$10.00'],
      ['2026-09-15', 'Traded', '-$62.40', 'Traded', '-$80.25'],
      ['2026-09-16', 'Traded', '$32.65', 'Skipped', '$0.00'],
    ]);
    expect(container.querySelectorAll('tbody app-timestamp-display[data-timestamp-mode="date-et"]').length).toBe(3);
  });

  it('subtracts nothing: no column or row of differences is made, and the totals are the server’s even when they disagree with the rows', async () => {
    const answer = comparison();
    await renderCompare(serving({ ...answer, overlap: { ...answer.overlap, a: { ...answer.overlap.a, net_profit_usd: '999.99' } } }));
    await screen.findByRole('region', { name: 'Each shared session (scrolls sideways)' });

    expect(screen.getAllByRole('columnheader').map((cell) => reads(cell)).filter((name) => /differ|gap|delta|change/i.test(name) && name !== 'Settings that differ')).toEqual([]);
    expect(totalsRows()[0]).toEqual(['Net profit', '$999.99', '-$70.25']);
  });

  it('says so, and sets nothing side by side, when the two runs share no session', async () => {
    const answer = comparison();
    await renderCompare(serving({ ...answer, overlap: { shared_sessions: 0, only_in_a: 5, only_in_b: 5, a: { net_profit_usd: '0.00', trades: 0, winning_sessions: 0 }, b: { net_profit_usd: '0.00', trades: 0, winning_sessions: 0 }, sessions: [] } }));

    expect(await screen.findByText('The two runs have no session in common, so there is nothing to set side by side.')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Each shared session (scrolls sideways)' })).toBeNull();
    expect(reads(screen.getByRole('region', { name: 'Sessions both runs tested' }).querySelector('p'))).toBe('Sessions in both runs: 0. Only in run A: 5. Only in run B: 5.');
  });
});

describe('comparing two runs, the server refuses', () => {
  it.each([
    [409, 'OPTIONS_BACKTEST_RUN_NOT_COMPLETE', 'Only complete runs can be compared, and run 33 (b) is failed.'],
    [409, 'OPTIONS_BACKTEST_RUNS_OF_DIFFERENT_UNDERLYINGS', 'Only runs of one underlying can be compared: run 30 is SPY and run 33 is QQQ.'],
    [409, 'OPTIONS_BACKTEST_RUN_SETUP_CANNOT_BE_READ', 'Runs can be compared only while both stored setups can be read, and the stored setup of run 33 (b) can no longer be read: lots: Extra inputs are not permitted.'],
    [404, 'OPTIONS_BACKTEST_RUN_NOT_FOUND', 'No run has the id 33 (run b).'],
  ])('shows the server’s own words for a %s (%s) and nothing else of the comparison', async (status, code, message) => {
    const detail = fakeRunDetail(vi, { compare: vi.fn(async () => { throw new RunDetailRefusedError(code, message, status); }) });
    await renderCompare(detail);

    expect(reads(await screen.findByRole('alert'))).toBe(message);
    expect(screen.queryByRole('region', { name: 'What differs between the two runs' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Sessions both runs tested' })).toBeNull();
  });

  it('reads again on request, and shows the comparison when the second reading works', async () => {
    let refusing = true;
    const compare = vi.fn(async () => {
      if (refusing) throw new RunDetailRefusedError('OPTIONS_BACKTEST_RUN_NOT_COMPLETE', 'Only complete runs can be compared, and run 33 (b) is running.', 409);
      return comparison();
    });
    await renderCompare(fakeRunDetail(vi, { compare }));
    await screen.findByRole('alert');
    refusing = false;

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    await screen.findByRole('region', { name: 'Sessions both runs tested' });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says plainly when the comparison cannot be read at all', async () => {
    await renderCompare(fakeRunDetail(vi, { compare: vi.fn(async () => { throw new Error('offline'); }) }));

    expect(reads(await screen.findByRole('alert'))).toBe('The comparison could not be read.');
  });
});

describe('comparing two runs, an answer that comes too late', () => {
  it('shows the comparison of the runs now asked for and not of the pair asked for before', async () => {
    const first = deferred<RunComparison>();
    const compare = vi.fn((a: number) => (a === 30 ? first.promise : Promise.resolve(comparison({ a: comparedRun({ id: 40, setup_name: 'Second pair' }), b: comparedRun({ id: 41, setup_name: null }) }))));
    const view = await renderCompare(fakeRunDetail(vi, { compare }));
    await lettingGo();

    await view.rerender({ inputs: { a: 40, b: 41 } });
    await screen.findByRole('list', { name: 'The two runs' });
    first.resolve(comparison({ a: comparedRun({ id: 30, setup_name: 'First pair' }) }));
    await lettingGo();

    const items = within(screen.getByRole('list', { name: 'The two runs' })).getAllByRole('listitem');
    expect(reads(items[0])).toContain('Second pair');
    expect(reads(items[0])).not.toContain('First pair');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Run 40 compared with run 41' })).toBeTruthy());
  });
});

describe('comparing two runs, another pair', () => {
  it('is read again when only run b is another run', async () => {
    const detail = serving();
    const view = await renderCompare(detail, 30, 33);
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    await view.rerender({ inputs: { a: 30, b: 34 } });

    await waitFor(() => expect(detail.compare).toHaveBeenLastCalledWith(30, 34));
    expect(detail.compare).toHaveBeenCalledTimes(2);
  });

  it('is read again when only run a is another run', async () => {
    const detail = serving();
    const view = await renderCompare(detail, 30, 33);
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    await view.rerender({ inputs: { a: 29, b: 33 } });

    await waitFor(() => expect(detail.compare).toHaveBeenLastCalledWith(29, 33));
  });
});

describe('comparing two runs, the keyboard', () => {
  it('can be put on the heading of the view, where the owner has just asked to be', async () => {
    const view = await renderCompare(serving());
    await screen.findByRole('region', { name: 'Sessions both runs tested' });

    view.fixture.componentInstance.focusHeading();

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Run 30 compared with run 33' })));
  });
});
