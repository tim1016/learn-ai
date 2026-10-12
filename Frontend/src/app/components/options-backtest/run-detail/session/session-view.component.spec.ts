import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../../../shared/date/et-midnight';
import { reads } from '../../testing/page-actions';
import { runDetail } from '../../testing/run-fixtures';
import { RunDetailRefusedError } from '../run-detail.service';
import type { SessionDetail } from '../run-detail.types';
import {
  CALL_585, CALL_CLOSES, PUT_585, SERIES_WORDS, sessionDetail, shownSeries, skippedSession, TUESDAY_OPEN, WEDNESDAY_OPEN, withheldSeries,
} from '../testing/detail-fixtures';
import { fakeRunDetail } from '../testing/fake-run-detail';
import { renderSession } from '../testing/render-session';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn(),
  LineSeries: 'LineSeries',
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

const ASKED = { openMs: TUESDAY_OPEN, origin: null };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function servedAs(day: SessionDetail) {
  return fakeRunDetail(vi, { session: vi.fn(async () => day) });
}

const facts = () => [...document.querySelectorAll('.facts > div')].map((each) => reads(each));
const tableRows = (name: string) => [...within(screen.getByRole('region', { name })).getAllByRole('row')].slice(1).map((row) => [...row.querySelectorAll('td')].map((cell) => reads(cell)));
const heading = () => screen.getByRole('heading', { name: 'One day' });

describe('one day, before one is chosen', () => {
  it('is a region of its own with a picker of the run’s days, and reads nothing yet', async () => {
    const { detail } = await renderSession(vi);

    expect(screen.getByRole('region', { name: 'One day' })).toBeTruthy();
    const picker = screen.getByRole('combobox', { name: 'Day' });
    expect([...picker.querySelectorAll('option')].map((option) => option.textContent)).toEqual([
      'Choose a day', '2026-09-14 · Traded', '2026-09-15 · Traded', '2026-09-16 · No Trade', '2026-09-17 · Skipped', '2026-09-18 · Traded',
    ]);
    expect(screen.getByText(/^Choose a day here, or press a day in the trades/).getAttribute('role')).toBe('status');
    expect(detail.session).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Close this day' })).toBeNull();
  });

  it('has no days to pick from when the server sent no figures', async () => {
    await renderSession(vi, { run: runDetail({ figures: null }) });

    expect([...screen.getByRole('combobox', { name: 'Day' }).querySelectorAll('option')].map((option) => option.textContent)).toEqual(['Choose a day']);
  });
});

describe('one day, the days to choose from', () => {
  // 00:30 New York time on the 16th is 23:30 on the 15th in Chicago and 18:30 on the 15th in Honolulu; 20:00 on the 15th is the 16th in Tokyo.
  it.each([
    ['00:30 on the 16th', etMidnightMs('2026-09-16') + 30 * 60_000, '2026-09-16'],
    ['20:00 on the 15th', etMidnightMs('2026-09-15') + 20 * 60 * 60_000, '2026-09-15'],
  ])('names a day by its New York date, whatever the zone of the viewer: an open at %s', async (_name, openMs, date) => {
    const run = runDetail();
    const figures = run.figures;
    if (figures === null || figures === undefined) throw new Error('The fixture run has no figures.');
    await renderSession(vi, { run: { ...run, figures: { ...figures, by_day: [{ ...figures.by_day[0], session_open_ms: openMs }] } } });

    expect([...screen.getByRole('combobox', { name: 'Day' }).querySelectorAll('option')].map((option) => option.textContent)).toEqual(['Choose a day', `${date} · Traded`]);
  });
});

describe('one day, choosing it', () => {
  it('reads the day the owner picks, for this run, and says it is reading while it waits', async () => {
    const waiting = deferred<SessionDetail>();
    const detail = fakeRunDetail(vi, { session: vi.fn(() => waiting.promise) });
    await renderSession(vi, { detail });

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Day' }), '2026-09-15 · Traded');

    expect((await screen.findByText('Reading the day…')).getAttribute('role')).toBe('status');
    expect(detail.session).toHaveBeenCalledWith(7, TUESDAY_OPEN);
    expect(screen.getByRole('button', { name: 'Close this day' })).toBeTruthy();
  });

  it('reads the day a trade or a row of the table asked for, with no further step', async () => {
    const { detail } = await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });

    await screen.findByText('Traded');
    expect(detail.session).toHaveBeenCalledTimes(1);
    expect(detail.session).toHaveBeenCalledWith(7, TUESDAY_OPEN);
    expect((screen.getByRole('combobox', { name: 'Day' }) as HTMLSelectElement).value).toBe('2026-09-15 · Traded');
  });
});

describe('one day, the stored facts', () => {
  it('shows the day, the hours of the session in New York time, how it came out, the cash on either side and the profit, as the server wrote them', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByText('Traded');

    expect(facts()).toEqual([
      'Day 2026-09-15',
      'Session 09:30:00 ET to 09:42:00 ET',
      'Status Traded',
      'Cash before $10,000.00',
      'Cash after $9,888.40',
      'Profit -$111.60',
    ]);
  });

  it('shows the trades of the day in a table of their own, without a control to open the day again', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('table', { name: 'Leg trades of this day' });

    expect(tableRows('Leg trades of this day (scrolls sideways)').map((row) => row.slice(1, 3))).toEqual([['call', 'first entry'], ['put', 'first entry']]);
    expect(screen.queryByRole('button', { name: /^Look at / })).toBeNull();
  });

  it('lists what happened in order, in New York time, with the kind through the shared label, the leg and its try, the contract, the price and the reason', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('region', { name: 'What happened, in order (scrolls sideways)' });

    const rows = tableRows('What happened, in order (scrolls sideways)');
    expect(rows.map((row) => row.slice(0, 2).concat(row[2], row[4], row[5]))).toEqual([
      ['09:31:00 ET', 'Picked', 'call, first entry', '', ''],
      ['09:31:00 ET', 'Picked', 'put, first entry', '', ''],
      ['09:31:00 ET', 'Pick Refused', 'wing, first entry', '', 'Strike Not Listed'],
      ['09:32:00 ET', 'Starting Price Taken', 'call, first entry', '$2.00', ''],
      ['09:34:00 ET', 'Entry Filled', 'put, first entry', '$1.42', ''],
      ['09:37:00 ET', 'Momentum Hit', 'call, first entry', '$2.50', ''],
      ['09:38:00 ET', 'Entry Filled', 'call, first entry', '$2.57', ''],
      ['09:40:00 ET', 'Stop Hit', 'call, first entry', '$1.85', ''],
      ['09:41:00 ET', 'Close Filled', 'call, first entry', '$1.82', ''],
      ['09:41:00 ET', 'Re Entry Earned', 'call, re-entry 1', '', ''],
      ['09:41:00 ET', 'Exit Time', 'put, first entry', '', 'Exit Time'],
      ['09:42:00 ET', 'Marked Not Sold', 'put, first entry', '$1.25', ''],
    ]);
  });

  it('names a contract in words where the server sent its parts, and shows none where an event is not about one', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('region', { name: 'What happened, in order (scrolls sideways)' });

    const rows = tableRows('What happened, in order (scrolls sideways)');
    expect(rows[0][3]).toContain("Oct 16 '26 585 Call");
    expect(rows[1][3]).toContain("Oct 16 '26 585 Put");
    expect(rows[2][3]).toBe('— none');
    expect(rows[9][3]).toBe('— none');
  });

  it('names a contract in words that only a chart series sent the parts of, because no trade of the day was in it', async () => {
    const day = sessionDetail({
      trades: [],
      contracts: [shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '2.10']])],
      events: [sessionDetail().events[3]],
      levels: [sessionDetail().levels[0]],
      fills: [],
      marks: [],
    });
    await renderSession(vi, { detail: servedAs(day), request: ASKED });
    await screen.findByRole('region', { name: 'What happened, in order (scrolls sideways)' });

    expect(tableRows('What happened, in order (scrolls sideways)')[0][3]).toContain("Oct 16 '26 585 Call");
    expect(tableRows('Levels (scrolls sideways)')[0][3]).toContain("Oct 16 '26 585 Call");
  });

  it('names a contract in words that only a trade of the day sent the parts of, because no chart series was sent for it', async () => {
    const day = sessionDetail({ contracts: [], events: sessionDetail().events.slice(0, 2), levels: [sessionDetail().levels[0]] });
    await renderSession(vi, { detail: servedAs(day), request: ASKED });
    await screen.findByRole('region', { name: 'What happened, in order (scrolls sideways)' });

    expect(tableRows('What happened, in order (scrolls sideways)').map((row) => row[3])).toEqual([expect.stringContaining("Oct 16 '26 585 Call"), expect.stringContaining("Oct 16 '26 585 Put")]);
    expect(tableRows('Levels (scrolls sideways)')[0][3]).toContain("Oct 16 '26 585 Call");
  });

  it('shows the symbol as it came for a contract the server sent no parts for', async () => {
    const day = sessionDetail({ events: [{ ...sessionDetail().events[0], contract: 'SPY261016C00999000' }] });
    await renderSession(vi, { detail: servedAs(day), request: ASKED });
    await screen.findByRole('region', { name: 'What happened, in order (scrolls sideways)' });

    expect(tableRows('What happened, in order (scrolls sideways)')[0][3]).toBe('SPY261016C00999000');
  });

  it('shows the words the server stored for an event exactly as it stored them, a date in them and all', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('region', { name: 'What happened, in order (scrolls sideways)' });

    expect(tableRows('What happened, in order (scrolls sideways)')[2][6]).toBe('No 590 call was listed for 2026-10-09 with a trade that day.');
    expect(tableRows('What happened, in order (scrolls sideways)').filter((row) => row[6] !== '').length).toBe(1);
  });

  it('says so when nothing happened on the day', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail({ events: [] })), request: ASKED });

    expect(await screen.findByText('Nothing happened on this day.')).toBeTruthy();
  });

  it('says so when no leg traded', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail({ trades: [] })), request: ASKED });

    expect(await screen.findByText('No leg traded on this day.')).toBeTruthy();
    expect(screen.queryByRole('table', { name: 'Leg trades of this day' })).toBeNull();
  });
});

describe('one day, the chart as text', () => {
  it('lists each level with the two instants it is drawn between, how it is drawn, and its price as the server wrote it', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('region', { name: 'Levels (scrolls sideways)' });

    expect(tableRows('Levels (scrolls sideways)').map((row) => [row[0], row[1], row[2], row[4], row[5], row[6]])).toEqual([
      ['Starting price', 'dotted line', 'call, first entry', '$2.00', '09:32:00 ET', '09:37:00 ET'],
      ['Momentum level', 'dashed line', 'call, first entry', '$2.50', '09:32:00 ET', '09:37:00 ET'],
      ['Stop level', 'long dashes', 'call, first entry', '$1.93', '09:38:00 ET', '09:40:00 ET'],
    ]);
  });

  it('lists each fill with the price it filled at and the price seen, and why a sale was made', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('region', { name: 'Fills (scrolls sideways)' });

    expect(tableRows('Fills (scrolls sideways)').map((row) => [row[0], row[1], row[2], row[4], row[5], row[6], row[7], row[8]])).toEqual([
      ['Bought', 'up arrow', 'put, first entry', '2', '09:34:00 ET', '$1.42', '$1.40', ''],
      ['Bought', 'up arrow', 'call, first entry', '1', '09:38:00 ET', '$2.57', '$2.55', ''],
      ['Sold', 'down arrow', 'call, first entry', '1', '09:41:00 ET', '$1.82', '$1.84', 'Stop'],
    ]);
  });

  it('lists a leg marked and not sold as a mark, in a list of its own and never among the fills', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('region', { name: 'Marks (scrolls sideways)' });

    expect(tableRows('Marks (scrolls sideways)').map((row) => [row[0], row[1], row[2], row[4], row[5]])).toEqual([
      ['Marked, not sold', 'square', 'put, first entry', '09:40:00 ET', '$1.25'],
    ]);
    expect(tableRows('Fills (scrolls sideways)').map((row) => row[0])).not.toContain('Marked, not sold');
  });

  it('says so when there is no level, no fill and no mark', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail({ levels: [], fills: [], marks: [] })), request: ASKED });

    expect(await screen.findByText('No levels were in force on this day.')).toBeTruthy();
    expect(screen.getByText('Nothing was bought or sold on this day.')).toBeTruthy();
    expect(screen.getByText('No leg was left unsold on this day.')).toBeTruthy();
  });

  it('describes each pane of the chart, top to bottom, with every line and mark and how it is drawn', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('list', { name: 'Panes of the chart, from the top' });

    const panes = [...screen.getByRole('list', { name: 'Panes of the chart, from the top' }).querySelectorAll(':scope > li')].map((pane) => [...pane.querySelectorAll('.entries li')].map((entry) => reads(entry)));
    expect(panes).toEqual([
      ['Minute closes (solid line)'],
      ['Minute closes (solid line)', '▲ Bought (up arrow)', '▼ Sold (down arrow)', 'Starting price (dotted line)', 'Momentum level (dashed line)', 'Stop level (long dashes)'],
      ['Minute closes (solid line)', '● A lone minute with a trade (dot)', '▲ Bought (up arrow)', '■ Marked, not sold (square)'],
    ]);
  });
});

describe('one day, the chart', () => {
  it('makes one chart for the day, once the day has been read, and hands it every series the plan has', async () => {
    const { chart } = await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('img', { name: /^Minute closes of SPY\. Contracts drawn: 2\./ });
    await waitFor(() => expect(chart.series.length).toBe(12));

    expect(chart.created.length).toBe(1);
  });

  it('takes the chart down when the day is closed', async () => {
    const { chart } = await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('img');

    fireEvent.click(screen.getByRole('button', { name: 'Close this day' }));

    await waitFor(() => expect(chart.chart.remove).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('img')).toBeNull();
  });
});

describe('one day, a series the server withholds', () => {
  const withheld = (reason: Parameters<typeof withheldSeries>[2]) => sessionDetail({ contracts: [withheldSeries(CALL_585.symbol, CALL_585, reason), shownSeries(CALL_585.symbol.replace('C', 'P'), { ...CALL_585, symbol: CALL_585.symbol.replace('C', 'P'), right: 'put' }, [[3, '1.40'], [4, '1.44']])] });

  it.each([
    ['session_skipped'], ['no_pin'], ['gone'], ['changed'], ['not_confirmed'], ['unreadable'], ['lake_unavailable'],
  ] as const)('says why a contract is not drawn in the server’s own words for %s, and still shows the stored facts, the events, the fills and the other series', async (reason) => {
    const { chart } = await renderSession(vi, { detail: servedAs(withheld(reason)), request: ASKED });
    await screen.findByRole('list', { name: 'Panes of the chart, from the top' });

    const list = screen.getByRole('region', { name: 'Not drawn' });
    expect(reads(within(list).getAllByRole('listitem')[0])).toContain(SERIES_WORDS[reason]);
    expect(reads(within(list).getAllByRole('listitem')[0])).toContain("SPY Oct 16 '26 585 Call");
    expect(reads(within(list).getAllByRole('listitem')[0])).toContain('Its levels, fills and marks are listed below.');
    // The stored facts and the lists are all there, and the withheld contract’s levels and fills are still listed.
    expect(facts()[0]).toBe('Day 2026-09-15');
    expect(tableRows('Fills (scrolls sideways)').length).toBe(3);
    expect(tableRows('Levels (scrolls sideways)').length).toBe(3);
    expect(tableRows('What happened, in order (scrolls sideways)').length).toBe(12);
    // And the chart is handed the other series only: two panes (SPY, and the other contract), none for the withheld one.
    expect(new Set(chart.series.map((each) => each.pane))).toEqual(new Set([0, 1]));
  });

  it('still lists the mark of a contract whose series is withheld, among the marks, though no pane of the chart is left to draw it on', async () => {
    const day = sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, CALL_CLOSES), withheldSeries(PUT_585.symbol, PUT_585, 'gone')] });
    const { chart } = await renderSession(vi, { detail: servedAs(day), request: ASKED });
    await screen.findByRole('list', { name: 'Panes of the chart, from the top' });

    expect(tableRows('Marks (scrolls sideways)').map((row) => [row[0], row[2], row[5]])).toEqual([['Marked, not sold', 'put, first entry', '$1.25']]);
    expect(new Set(chart.series.map((each) => each.pane))).toEqual(new Set([0, 1]));
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain('Marks drawn: 0.');
  });

  it('names the reason through the shared label, and says nothing in the page’s own words for it', async () => {
    await renderSession(vi, { detail: servedAs(withheld('changed')), request: ASKED });
    await screen.findByRole('region', { name: 'Not drawn' });

    expect(reads(within(screen.getByRole('region', { name: 'Not drawn' })).getAllByRole('listitem')[0])).toBe("SPY Oct 16 '26 585 Call : Changed The stored bars have changed since this run. Its levels, fills and marks are listed below.");
  });

  it('draws no chart at all and gives the server’s words for a skipped day, with the facts of it', async () => {
    const { chart } = await renderSession(vi, { detail: servedAs(skippedSession()), request: ASKED });
    await screen.findByText('Skipped because');

    expect(facts()).toEqual([
      'Day 2026-09-15',
      'Session 09:30:00 ET to 09:42:00 ET',
      'Status Skipped',
      'Skipped because Window Does Not Fit',
      'Cash before $10,000.00',
      'Cash after $10,000.00',
      'Profit $0.00',
    ]);
    expect(screen.queryByRole('img')).toBeNull();
    expect(chart.created).toEqual([]);
    expect(reads(within(screen.getByRole('region', { name: 'Not drawn' })).getAllByRole('listitem')[0])).toContain(SERIES_WORDS.session_skipped);
    expect(screen.getByText('No leg traded on this day.')).toBeTruthy();
    expect(screen.getByText('Nothing happened on this day.')).toBeTruthy();
  });

  it('shows a day with no trade as it is, and draws the underlying', async () => {
    const day = sessionDetail({ status: 'no_trade', trades: [], events: [], contracts: [], levels: [], fills: [], marks: [], profit_usd: '0.00', cash_after_usd: '10000.00' });
    const { chart } = await renderSession(vi, { detail: servedAs(day), request: ASKED });
    await screen.findByText('No Trade');

    expect(screen.getByText('No leg traded on this day.')).toBeTruthy();
    expect(screen.getByRole('img', { name: /^Minute closes of SPY\. Contracts drawn: 0\./ })).toBeTruthy();
    expect(new Set(chart.series.map((each) => each.pane))).toEqual(new Set([0]));
  });
});

describe('one day, a run whose setup can no longer be read', () => {
  it('says what is missing and why, in the server’s words, and shows everything else', async () => {
    const why = 'lots: Extra inputs are not permitted.';
    await renderSession(vi, { detail: servedAs(sessionDetail({ setup_cannot_open_reason: why })), request: ASKED });

    const notice = await screen.findByText(/^The stored setup of this run can no longer be read/);
    expect(reads(notice)).toBe(`The stored setup of this run can no longer be read, so one kind of line is missing here: the momentum level of an attempt that never bought. Why: ${why}`);
    expect(notice.getAttribute('role')).toBe('status');
    expect(facts()[0]).toBe('Day 2026-09-15');
  });

  it('says nothing about a setup when the run’s is fine', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByText('Traded');

    expect(screen.queryByText(/can no longer be read/)).toBeNull();
  });
});

describe('one day, refusals', () => {
  it('shows the server’s words when the run did not test the day, and reads again on request', async () => {
    let refusing = true;
    const session = vi.fn(async () => {
      if (refusing) throw new RunDetailRefusedError('OPTIONS_BACKTEST_SESSION_NOT_IN_RUN', 'Run 7 did not test a session that opened at 1789478400000.', 404);
      return sessionDetail();
    });
    await renderSession(vi, { detail: fakeRunDetail(vi, { session }), request: ASKED });

    expect(reads(await screen.findByRole('alert'))).toBe('Run 7 did not test a session that opened at 1789478400000.');
    refusing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    await screen.findByText('Traded');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows the server’s words when the run is not complete, or the stored day cannot be drawn', async () => {
    const detail = fakeRunDetail(vi, {
      session: vi.fn(async () => {
        throw new RunDetailRefusedError('OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT', 'The stored trades and events of this session do not agree, so it cannot be drawn: leg call.', 500);
      }),
    });
    await renderSession(vi, { detail, request: ASKED });

    expect(reads(await screen.findByRole('alert'))).toBe('The stored trades and events of this session do not agree, so it cannot be drawn: leg call.');
  });

  it('says plainly when the day cannot be read at all', async () => {
    await renderSession(vi, { detail: fakeRunDetail(vi, { session: vi.fn(async () => { throw new Error('offline'); }) }), request: ASKED });

    expect(reads(await screen.findByRole('alert'))).toBe('The day could not be read.');
  });
});

describe('one day, an answer that comes too late', () => {
  it('shows the day the owner picked last, not the one picked before, when the earlier answer arrives last', async () => {
    const tuesday = deferred<SessionDetail>();
    const wednesday = deferred<SessionDetail>();
    const session = vi.fn((_runId: number, openMs: number) => (openMs === TUESDAY_OPEN ? tuesday.promise : wednesday.promise));
    await renderSession(vi, { detail: fakeRunDetail(vi, { session }), request: ASKED });
    await screen.findByText('Reading the day…');

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Day' }), '2026-09-16 · No Trade');
    wednesday.resolve(sessionDetail({ session_open_ms: WEDNESDAY_OPEN, session_close_ms: WEDNESDAY_OPEN + 12 * 60_000, status: 'no_trade', profit_usd: '0.00', trades: [], events: [] }));
    await screen.findByText('No Trade');
    tuesday.resolve(sessionDetail({ profit_usd: '-111.60' }));
    await waitFor(() => expect(session).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(facts()[0]).toBe('Day 2026-09-16');
    expect(facts()).toContain('Profit $0.00');
    expect(screen.queryByText('-$111.60')).toBeNull();
  });

  it('shows the second run’s day and not the first run’s when the first run’s answer arrives last', async () => {
    const first = deferred<SessionDetail>();
    const session = vi.fn((runId: number) => (runId === 7 ? first.promise : Promise.resolve(sessionDetail({ run_id: 8, profit_usd: '5.00' }))));
    const { view } = await renderSession(vi, { detail: fakeRunDetail(vi, { session }), request: ASKED });
    await screen.findByText('Reading the day…');

    await view.rerender({ inputs: { run: runDetail({ id: 8 }) }, partialUpdate: true });
    await waitFor(() => expect(facts()).toContain('Profit $5.00'));
    first.resolve(sessionDetail({ run_id: 7, profit_usd: '-111.60' }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(facts()).toContain('Profit $5.00');
    expect(session).toHaveBeenCalledWith(8, TUESDAY_OPEN);
  });
});

describe('one day, the keyboard', () => {
  it('moves into the view, to its heading, when a day is asked for from outside', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });

    await waitFor(() => expect(document.activeElement).toBe(heading()));
  });

  it('does not take the keyboard when the owner picks a day with the picker', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()) });
    const picker = screen.getByRole('combobox', { name: 'Day' });
    picker.focus();

    await userEvent.selectOptions(picker, '2026-09-15 · Traded');
    await screen.findByText('Traded');

    expect(document.activeElement).toBe(picker);
  });

  it('gives the keyboard back to the control that asked, when the day is closed', async () => {
    const origin = document.createElement('button');
    origin.textContent = 'Look at 2026-09-15';
    document.body.appendChild(origin);
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: { openMs: TUESDAY_OPEN, origin } });
    await screen.findByText('Traded');

    fireEvent.click(screen.getByRole('button', { name: 'Close this day' }));

    await waitFor(() => expect(document.activeElement).toBe(origin));
    expect(screen.queryByText('Traded')).toBeNull();
    origin.remove();
  });

  it('puts the keyboard on the picker when the control that asked is gone', async () => {
    const origin = document.createElement('button');
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: { openMs: TUESDAY_OPEN, origin } });
    await screen.findByText('Traded');

    fireEvent.click(screen.getByRole('button', { name: 'Close this day' }));

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Day' })));
  });

  it('puts the keyboard on the picker when a day the owner picked is closed and nothing asked for it', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()) });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Day' }), '2026-09-15 · Traded');
    await screen.findByText('Traded');

    fireEvent.click(screen.getByRole('button', { name: 'Close this day' }));

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Day' })));
    expect((screen.getByRole('combobox', { name: 'Day' }) as HTMLSelectElement).value).toBe('Choose a day');
  });
});

describe('one day, time', () => {
  it('names the day by its New York date and every time of day in New York time, whatever the viewer’s zone', async () => {
    const { view } = await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByText('Traded');

    const stamps = [...view.container.querySelectorAll('app-timestamp-display')].map((stamp) => stamp.getAttribute('data-timestamp-mode'));
    expect(stamps.length).toBeGreaterThan(20);
    expect(stamps.every((mode) => mode === 'et' || mode === 'date-et')).toBe(true);
    expect(facts()[0]).toBe('Day 2026-09-15');
  });
});
