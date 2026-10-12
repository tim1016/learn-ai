import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../../../shared/date/et-midnight';
import { sessionOpenMs } from '../../../../shared/date-picker/testing/fake-trading-calendar';
import type { RunDetail } from '../../options-backtest.types';
import { lettingGo, reads } from '../../testing/page-actions';
import { runDetail, runWithoutSetup } from '../../testing/run-fixtures';
import { RunDetailRefusedError, RunDetailService } from '../run-detail.service';
import type { LegTrade, TradeQuery, TradesPage } from '../run-detail.types';
import { FOUR_TRADES, stoppedTrade, tradesPage } from '../testing/detail-fixtures';
import { fakeRunDetail, type FakeRunDetail } from '../testing/fake-run-detail';
import { RunTradesComponent } from './run-trades.component';

async function renderTrades(detail: FakeRunDetail, run: RunDetail = runDetail()) {
  const view = await render(RunTradesComponent, { inputs: { run }, providers: [{ provide: RunDetailService, useValue: detail }] });
  return view;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

/** A page of `count` trades, however many there are in all, and where the server says the next page starts. */
function pageOf(count: number, changes: Partial<TradesPage> = {}): TradesPage {
  const trades: LegTrade[] = Array.from({ length: count }, (_, at) => stoppedTrade({ trade_number: at, leg_id: `leg_${at}` }));
  return tradesPage({ trades, ...changes });
}

const lastQuery = (detail: FakeRunDetail): TradeQuery | undefined => detail.trades.mock.lastCall?.[1];
const legsOnScreen = () => screen.queryAllByRole('row').slice(1).map((row) => reads(row.querySelectorAll('td')[1] ?? null));

describe('the trades of a run, reading them', () => {
  it('reads the first page of the run on screen, 100 trades at most, with no filter', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    await renderTrades(detail);

    await screen.findByRole('table', { name: 'Leg trades' });
    expect(detail.trades).toHaveBeenCalledTimes(1);
    expect(detail.trades).toHaveBeenCalledWith(7, { sessionOpenMs: null, legId: '', limit: 100, offset: 0 });
  });

  it('shows the trades in the order the server gave them, under the heading Trades', async () => {
    await renderTrades(fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) }));

    await screen.findByRole('table', { name: 'Leg trades' });
    expect(screen.getByRole('region', { name: 'Trades' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Trades' })).toBeTruthy();
    expect(legsOnScreen()).toEqual(['call', 'call', 'put', 'put']);
  });

  it('says it is reading while the answer is on its way', async () => {
    const waiting = deferred<TradesPage>();
    await renderTrades(fakeRunDetail(vi, { trades: vi.fn(() => waiting.promise) }));

    expect((await screen.findByText('Reading the trades…')).getAttribute('role')).toBe('status');
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('says so when the run made no trades', async () => {
    await renderTrades(fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage({ trades: [], total: 0 })) }));

    expect((await screen.findByText('This run made no trades.')).getAttribute('role')).toBe('status');
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('shows the server’s words when it refuses, and reads again on request', async () => {
    let refusing = true;
    const trades = vi.fn(async () => {
      if (refusing) throw new RunDetailRefusedError('OPTIONS_BACKTEST_RUN_NOT_COMPLETE', 'Run 7 is failed. Only a complete run has trades and sessions.', 409);
      return tradesPage();
    });
    await renderTrades(fakeRunDetail(vi, { trades }));

    const alert = await screen.findByRole('alert');
    expect(reads(alert)).toBe('Run 7 is failed. Only a complete run has trades and sessions.');
    refusing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    await screen.findByRole('table', { name: 'Leg trades' });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(trades).toHaveBeenCalledTimes(2);
  });

  it('says plainly when the trades cannot be read at all', async () => {
    await renderTrades(fakeRunDetail(vi, { trades: vi.fn(async () => { throw new Error('offline'); }) }));

    expect(reads(await screen.findByRole('alert'))).toBe('The trades could not be read.');
  });

  it('puts the export beside the heading, for the run on screen', async () => {
    await renderTrades(fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) }));

    expect(screen.getByRole('button', { name: 'Export trades (CSV)' })).toBeTruthy();
  });
});

describe('the trades of a run, paging with the server’s numbers', () => {
  const FIRST = pageOf(100, { total: 250, offset: 0, next_offset: 90 });
  const SECOND = pageOf(100, { total: 250, offset: 90, next_offset: 190 });
  const THIRD = pageOf(60, { total: 250, offset: 190, next_offset: null });
  const bySkip = (offset: number | undefined) => (offset === 0 ? FIRST : offset === 90 ? SECOND : THIRD);

  async function renderPaged() {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async (_runId: number, query?: TradeQuery) => bySkip(query?.offset)) });
    await renderTrades(detail);
    await screen.findByText('Trades 1 to 100 of 250');
    return detail;
  }

  it('says where the page sits among all of them, from the served offset and total', async () => {
    await renderPaged();

    expect(screen.getByRole('navigation', { name: 'Pages of trades' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Next page' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Previous page' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'First page' })).toBeNull();
  });

  it('asks for the next page at exactly the offset the server named, and not at the offset it would work out', async () => {
    const detail = await renderPaged();

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));

    await screen.findByText('Trades 91 to 190 of 250');
    expect(lastQuery(detail)).toEqual({ sessionOpenMs: null, legId: '', limit: 100, offset: 90 });
  });

  it('reaches the last page, where there is no next page, and shows the rows it holds', async () => {
    await renderPaged();

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Trades 91 to 190 of 250');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));

    await screen.findByText('Trades 191 to 250 of 250');
    expect(screen.queryByRole('button', { name: 'Next page' })).toBeNull();
    expect(legsOnScreen().length).toBe(60);
  });

  it('goes back to the page before, at the offset that page was read at', async () => {
    const detail = await renderPaged();
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Trades 91 to 190 of 250');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Trades 191 to 250 of 250');

    fireEvent.click(screen.getByRole('button', { name: 'Previous page' }));

    await screen.findByText('Trades 91 to 190 of 250');
    expect(lastQuery(detail)?.offset).toBe(90);
  });

  it('goes back to the first page from any page', async () => {
    const detail = await renderPaged();
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Trades 91 to 190 of 250');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Trades 191 to 250 of 250');

    fireEvent.click(screen.getByRole('button', { name: 'First page' }));

    await screen.findByText('Trades 1 to 100 of 250');
    expect(lastQuery(detail)?.offset).toBe(0);
    expect(screen.queryByRole('button', { name: 'Previous page' })).toBeNull();
  });

  it('never shows a page as the whole: 100 rows of 12,600 are labelled as that', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => pageOf(100, { total: 12600, next_offset: 100 })) });
    await renderTrades(detail);

    expect(await screen.findByText('Trades 1 to 100 of 12,600')).toBeTruthy();
    expect(legsOnScreen().length).toBe(100);
  });

  it('does not show the previous page’s rows while the next one is on its way', async () => {
    const second = deferred<TradesPage>();
    const detail = fakeRunDetail(vi, { trades: vi.fn((_runId: number, query?: TradeQuery) => (query?.offset === 0 ? Promise.resolve(FIRST) : second.promise)) });
    await renderTrades(detail);
    await screen.findByText('Trades 1 to 100 of 250');

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));

    await screen.findByText('Reading the trades…');
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText('Trades 1 to 100 of 250')).toBeNull();
  });
});

describe('the trades of a run, filters', () => {
  it('lists the run’s days and filters by the one chosen, from the first page', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    await renderTrades(detail);
    await screen.findByRole('table', { name: 'Leg trades' });
    const days = screen.getByRole('combobox', { name: /^Days/ });

    expect([...days.querySelectorAll('option')].map((option) => option.textContent)).toEqual(['All days', '2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18']);
    await userEvent.selectOptions(days, '2026-09-16');

    await waitFor(() => expect(lastQuery(detail)).toEqual({ sessionOpenMs: sessionOpenMs('2026-09-16'), legId: '', limit: 100, offset: 0 }));
  });

  // 00:30 New York time on the 16th is 23:30 on the 15th in Chicago; 20:00 on the 15th is the 16th in Tokyo.
  it.each([
    ['00:30 on the 16th', etMidnightMs('2026-09-16') + 30 * 60_000, '2026-09-16'],
    ['20:00 on the 15th', etMidnightMs('2026-09-15') + 20 * 60 * 60_000, '2026-09-15'],
  ])('names each day to filter by with its New York date, whatever the viewer’s zone: an open at %s', async (_name, openMs, date) => {
    const run = runDetail();
    const figures = run.figures;
    if (figures === null || figures === undefined) throw new Error('The fixture run has no figures.');
    await renderTrades(fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) }), { ...run, figures: { ...figures, by_day: [{ ...figures.by_day[0], session_open_ms: openMs }] } });
    await screen.findByRole('table', { name: 'Leg trades' });

    expect([...screen.getByRole('combobox', { name: /^Days/ }).querySelectorAll('option')].map((option) => option.textContent)).toEqual(['All days', date]);
  });

  it('lists the legs of the run’s setup and filters by the one chosen', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    await renderTrades(detail);
    await screen.findByRole('table', { name: 'Leg trades' });
    const legs = screen.getByRole('combobox', { name: /^Legs/ });

    expect([...legs.querySelectorAll('option')].map((option) => option.textContent)).toEqual(['All legs', 'call', 'put']);
    await userEvent.selectOptions(legs, 'put');

    await waitFor(() => expect(lastQuery(detail)).toEqual({ sessionOpenMs: null, legId: 'put', limit: 100, offset: 0 }));
  });

  it('goes back to All days and All legs when both are chosen again', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    await renderTrades(detail);
    await screen.findByRole('table', { name: 'Leg trades' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Days/ }), '2026-09-16');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Legs/ }), 'put');

    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Days/ }), 'All days');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Legs/ }), 'All legs');

    await waitFor(() => expect(lastQuery(detail)).toEqual({ sessionOpenMs: null, legId: '', limit: 100, offset: 0 }));
  });

  it('starts again from the first page when a filter changes', async () => {
    const detail = fakeRunDetail(vi, {
      trades: vi.fn(async (_runId: number, query?: TradeQuery) => pageOf(100, { total: 250, offset: query?.offset ?? 0, next_offset: query?.offset === 0 ? 90 : 190 })),
    });
    await renderTrades(detail);
    await screen.findByText('Trades 1 to 100 of 250');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Trades 91 to 190 of 250');

    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Legs/ }), 'put');

    await waitFor(() => expect(lastQuery(detail)).toEqual({ sessionOpenMs: null, legId: 'put', limit: 100, offset: 0 }));
    expect(screen.queryByRole('button', { name: 'Previous page' })).toBeNull();
  });

  it('starts again from the first page when the day changes', async () => {
    const detail = fakeRunDetail(vi, {
      trades: vi.fn(async (_runId: number, query?: TradeQuery) => pageOf(100, { total: 250, offset: query?.offset ?? 0, next_offset: query?.offset === 0 ? 90 : 190 })),
    });
    await renderTrades(detail);
    await screen.findByText('Trades 1 to 100 of 250');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText('Trades 91 to 190 of 250');

    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Days/ }), '2026-09-16');

    await waitFor(() => expect(lastQuery(detail)).toEqual({ sessionOpenMs: sessionOpenMs('2026-09-16'), legId: '', limit: 100, offset: 0 }));
    expect(screen.queryByRole('button', { name: 'Previous page' })).toBeNull();
  });

  it('offers a box to type a leg in when the run’s setup cannot be read, and filters by what is typed', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    await renderTrades(detail, runWithoutSetup());
    await screen.findByRole('table', { name: 'Leg trades' });

    expect(screen.queryByRole('combobox', { name: /^Legs/ })).toBeNull();
    const box = screen.getByRole('textbox', { name: 'Leg' });
    fireEvent.change(box, { target: { value: '  wing ' } });

    await waitFor(() => expect(lastQuery(detail)).toEqual({ sessionOpenMs: null, legId: 'wing', limit: 100, offset: 0 }));
  });

  it('shows the server’s refusal in its words when a filter names nothing, and keeps the filters so they can be changed', async () => {
    const trades = vi.fn(async (_runId: number, query?: TradeQuery) => {
      if (query?.legId === 'wing') throw new RunDetailRefusedError('OPTIONS_BACKTEST_LEG_NOT_IN_RUN', "Run 7 has no leg 'wing'.", 404);
      return tradesPage();
    });
    await renderTrades(fakeRunDetail(vi, { trades }), runWithoutSetup());
    await screen.findByRole('table', { name: 'Leg trades' });

    fireEvent.change(screen.getByRole('textbox', { name: 'Leg' }), { target: { value: 'wing' } });

    expect(reads(await screen.findByRole('alert'))).toBe("Run 7 has no leg 'wing'.");
    expect((screen.getByRole('textbox', { name: 'Leg' }) as HTMLInputElement).value).toBe('wing');
    fireEvent.change(screen.getByRole('textbox', { name: 'Leg' }), { target: { value: 'put' } });
    await screen.findByRole('table', { name: 'Leg trades' });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says a filter matched no trade, when a page comes back empty under one', async () => {
    const trades = vi.fn(async (_runId: number, query?: TradeQuery) => (query?.legId === 'put' ? tradesPage({ trades: [], total: 0 }) : tradesPage()));
    await renderTrades(fakeRunDetail(vi, { trades }));
    await screen.findByRole('table', { name: 'Leg trades' });

    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Legs/ }), 'put');

    expect((await screen.findByText('No trade matches these filters.')).getAttribute('role')).toBe('status');
  });

  it('has no day to filter by when the server sent no figures, and still reads the trades', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    await renderTrades(detail, runDetail({ figures: null }));

    await screen.findByRole('table', { name: 'Leg trades' });
    const days = screen.getByRole('combobox', { name: /^Days/ });
    expect([...days.querySelectorAll('option')].map((option) => option.textContent)).toEqual(['All days']);
  });
});

describe('the trades of a run, an answer that comes too late', () => {
  it('shows the later page and not the earlier one when the earlier answer arrives last', async () => {
    const early = deferred<TradesPage>();
    const later = deferred<TradesPage>();
    const trades = vi.fn((_runId: number, query?: TradeQuery) => (query?.legId === 'put' ? later.promise : early.promise));
    await renderTrades(fakeRunDetail(vi, { trades }));
    await lettingGo();

    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Legs/ }), 'put');
    later.resolve(pageOf(1, { total: 1, trades: [stoppedTrade({ leg_id: 'only_put' })] }));
    await screen.findByRole('table', { name: 'Leg trades' });
    early.resolve(pageOf(2, { total: 2, trades: [stoppedTrade({ leg_id: 'early_a' }), stoppedTrade({ leg_id: 'early_b', trade_number: 1 })] }));
    await lettingGo();

    expect(legsOnScreen()).toEqual(['only_put']);
  });

  it('shows the second run’s trades and not the first run’s when the first run’s answer arrives last', async () => {
    const first = deferred<TradesPage>();
    const second = deferred<TradesPage>();
    const trades = vi.fn((runId: number) => (runId === 7 ? first.promise : second.promise));
    const view = await renderTrades(fakeRunDetail(vi, { trades }));
    await lettingGo();

    await view.rerender({ inputs: { run: runDetail({ id: 8 }) } });
    second.resolve(pageOf(1, { run_id: 8, total: 1, trades: [stoppedTrade({ leg_id: 'run_eight' })] }));
    await screen.findByRole('table', { name: 'Leg trades' });
    first.resolve(pageOf(1, { run_id: 7, total: 1, trades: [stoppedTrade({ leg_id: 'run_seven' })] }));
    await lettingGo();

    expect(legsOnScreen()).toEqual(['run_eight']);
  });

  it('starts a new run on the first page with no filter, whatever the run before it had chosen', async () => {
    const detail = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    const view = await renderTrades(detail);
    await screen.findByRole('table', { name: 'Leg trades' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /^Legs/ }), 'put');
    await waitFor(() => expect(lastQuery(detail)?.legId).toBe('put'));

    await view.rerender({ inputs: { run: runDetail({ id: 8 }) } });

    await waitFor(() => expect(detail.trades).toHaveBeenLastCalledWith(8, { sessionOpenMs: null, legId: '', limit: 100, offset: 0 }));
    expect((screen.getByRole('combobox', { name: /^Legs/ }) as HTMLSelectElement).value).toBe('');
  });
});

describe('the trades of a run, with the table inside', () => {
  it('hands the four trades to the table whole, each with its own figures', async () => {
    await renderTrades(fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage({ trades: [...FOUR_TRADES] })) }));

    await screen.findByRole('table', { name: 'Leg trades' });
    expect(screen.getAllByRole('row').length).toBe(5);
  });
});
