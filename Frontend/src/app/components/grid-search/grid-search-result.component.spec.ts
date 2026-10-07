import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GridSearchResultComponent } from './grid-search-result.component';
import { RESEARCH_DEFAULT_FILL_MODE } from '../../models/fill-mode';
import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import { JobsService } from '../../services/jobs.service';
import { etMidnightMs } from '../../shared/date/et-midnight';
import { fakeCharts } from '../golden-search/testing/fake-charts';
import { GridSearchService } from './grid-search.service';
import { gridSearchDetail as detail } from './testing/fixtures';
import type { CellPageQuery, GridSearchCell, GridSearchCellCurves, GridSearchCellPage, GridSearchDetail } from './grid-search.types';

function cell(overrides: Partial<GridSearchCell> = {}): GridSearchCell {
  return {
    params_hash: 'h1',
    params: { short_window: 10, long_window: 30 },
    status: 'completed',
    attempt: 1,
    total_trades: 12,
    net_profit: 120.5,
    total_return_pct: 1.2,
    sharpe_ratio: 0.8,
    max_drawdown_pct: 3.4,
    win_rate: 0.5,
    bars_consumed: 1000,
    error: null,
    exploratory: false,
    completed_at_ms: 1704172600000,
    has_curve: true,
    is_leader: false,
    eligible: true,
    ...overrides,
  };
}

function page(cells: GridSearchCell[], overrides: Partial<GridSearchCellPage> = {}): GridSearchCellPage {
  return { total: cells.length, page: 1, page_size: 25, sort_by: 'sharpe_ratio', direction: 'desc', without_curve: 0, cells, ...overrides };
}

/** Each asked cell's curve over two sessions, ending 5% up after a 1% dip from its start. */
function curves(hashes: readonly string[]): GridSearchCellCurves {
  return {
    sessions_ms: [etMidnightMs('2024-01-02'), etMidnightMs('2024-01-03')],
    curves: hashes.map((params_hash) => ({ params_hash, first_session: 0, cumulative_return: [-0.01, 0.05], drawdown: [-0.01, 0], deepest_drawdown: -0.01 })),
    not_recorded: [],
  };
}

async function renderResult(service: Partial<GridSearchService>, inputs: { maxDrawn?: number } = {}) {
  return render(GridSearchResultComponent, {
    inputs: { searchId: 'abc', pollMs: 0, ...inputs },
    providers: [
      provideRouter([]),
      ...fakeCharts().providers,
      { provide: JobsService, useValue: { cancelJob: vi.fn(async () => undefined) } },
      {
        provide: GridSearchService,
        useValue: {
          get: vi.fn(async (_id: string) => detail()),
          cells: vi.fn(async (_id: string, _query: CellPageQuery) => page([cell()])),
          cellCurves: vi.fn(async (_id: string, hashes: readonly string[]) => curves(hashes)),
          delete: vi.fn(async (_id: string) => undefined),
          finish: vi.fn(async () => 'job-2'),
          ...service,
        },
      },
    ],
  });
}

/** The leader (h2, the fixture detail's leader) above one other cell. */
function leaderAndAnother(): GridSearchCell[] {
  return [cell({ params_hash: 'h2', params: { short_window: 5, long_window: 30 }, is_leader: true, sharpe_ratio: 1.9 }), cell()];
}

function drawnChart(): HTMLElement {
  return within(screen.getByRole('region', { name: 'Equity curves' })).getByRole('img');
}

describe('GridSearchResultComponent', () => {
  it('states that every figure is in-sample and routes into Walk-Forward with the search', async () => {
    await renderResult({});

    await waitFor(() => expect(screen.getByRole('note').textContent).toMatch(/in-sample/i));
    const link = screen.getByRole('link', { name: /walk-forward/i }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toContain('/walk-forward?search=abc');
  });

  it.each(['signal_bar_close', 'next_bar_open'] as const)("names the fill rule the search ran under (%s), not today's default (#2599)", async (ranUnder) => {
    const get = vi.fn(async (_id: string) => detail({ request: { ...detail().request, fill_mode: ranUnder } }));
    await renderResult({ get });

    expect(await screen.findByText(new RegExp(formatReceiptLabel(ranUnder)))).not.toBeNull();
    expect(screen.queryByText(new RegExp(formatReceiptLabel(RESEARCH_DEFAULT_FILL_MODE)))).toBeNull();
  });

  it('marks the leader, the ranking measure, and labels zero-trade and failed cells', async () => {
    const cells = vi.fn(async (_id: string, _query: CellPageQuery) => page([
      cell({ params_hash: 'h2', params: { short_window: 5, long_window: 30 }, is_leader: true, sharpe_ratio: 1.9 }),
      cell({ params_hash: 'h3', total_trades: 0, eligible: false, sharpe_ratio: null }),
      cell({ params_hash: 'h4', status: 'failed', error: 'boom', eligible: false, sharpe_ratio: null, total_trades: 0 }),
    ]));
    await renderResult({ cells });

    await waitFor(() => expect(screen.getByText('Leader')).not.toBeNull());
    expect(screen.getByText('Zero trades')).not.toBeNull();
    expect(screen.getByText('Failed')).not.toBeNull();
    expect(screen.getByText(/short_window=5, long_window=30/)).not.toBeNull();
    const sortedHeader = screen.getByRole('columnheader', { name: /sharpe/i });
    expect(sortedHeader.getAttribute('aria-sort')).toBe('descending');
  });

  it('draws the leader when the search opens, adds the cells you tick, reads each curve once, and clears', async () => {
    const cellCurves = vi.fn(async (_id: string, hashes: readonly string[]) => curves(hashes));
    await renderResult({ cells: vi.fn(async () => page(leaderAndAnother())), cellCurves });

    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('1 cell drawn'));
    expect((screen.getByRole('checkbox', { name: 'Draw short_window=5, long_window=30' }) as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByRole('checkbox', { name: 'Draw short_window=10, long_window=30' }));
    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('2 cells drawn'));
    // The tick asks only for the curve the page does not hold yet.
    expect(cellCurves.mock.calls).toEqual([['abc', ['h2']], ['abc', ['h1']]]);
    expect(screen.getByText('2 of at most 200 drawn')).not.toBeNull();
    // The table names the leader; a plain cell is named by its settings alone.
    const panel = screen.getByRole('region', { name: 'Equity curves' });
    fireEvent.click(within(panel).getByRole('button', { name: 'Show as table' }));
    const rows = within(panel).getAllByRole('row').map((row) => [...row.querySelectorAll('th, td')].map((item) => item.textContent?.trim()));
    expect(rows).toEqual([
      ['Cell', 'Settings', 'First session', 'Last session', 'Ended at', 'Deepest fall'],
      ['Leader', 'short_window=5, long_window=30', '2024-01-02', '2024-01-03', '+5.0%', '-1.0%'],
      ['—', 'short_window=10, long_window=30', '2024-01-02', '2024-01-03', '+5.0%', '-1.0%'],
    ]);

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(screen.getByText('Tick cells in the table below to draw them here.')).not.toBeNull();
    // Clearing is the owner's choice: the leader does not come back on the next read.
    expect((screen.getByRole('checkbox', { name: 'Draw short_window=5, long_window=30' }) as HTMLInputElement).checked).toBe(false);
  });

  it('draws the leader though the page listed does not hold its row', async () => {
    // The fixture search's leader is h2; a sort or a page turn has left only h1 on the table.
    const cellCurves = vi.fn(async (_id: string, hashes: readonly string[]) => curves(hashes));
    await renderResult({ cells: vi.fn(async () => page([cell()])), cellCurves });

    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('1 cell drawn'));
    expect(cellCurves.mock.calls).toEqual([['abc', ['h2']]]);
    const panel = screen.getByRole('region', { name: 'Equity curves' });
    fireEvent.click(within(panel).getByRole('button', { name: 'Show as table' }));
    expect(within(panel).getByRole('cell', { name: 'short_window=5, long_window=30' })).not.toBeNull();
    // Ticking the listed cell adds it beside the leader.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Draw short_window=10, long_window=30' }));
    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('2 cells drawn'));
  });

  it('reads a cell’s curve again for another search that holds the same cell', async () => {
    // A walk-forward fold's training and test sweeps hold the same cells: one's curve is never shown for the other.
    const cellCurves = vi.fn(async (_id: string, hashes: readonly string[]) => curves(hashes));
    const get = vi.fn(async (id: string) => detail({ id }));
    const view = await renderResult({ get, cells: vi.fn(async () => page(leaderAndAnother())), cellCurves });
    await waitFor(() => expect(cellCurves.mock.calls).toEqual([['abc', ['h2']]]));

    view.fixture.componentRef.setInput('searchId', 'xyz');

    await waitFor(() => expect(cellCurves.mock.calls).toEqual([['abc', ['h2']], ['xyz', ['h2']]]));
  });

  it('does not ask a search just opened for the last search’s leader, and draws its own once it has one', async () => {
    // "abc" is open with its leader drawn; "xyz" is opened while its record is still on its way, then finishes with the same settings in the lead.
    let arrive: (row: GridSearchDetail) => void = () => undefined;
    const pending = new Promise<GridSearchDetail>((resolve) => (arrive = resolve));
    const get = vi.fn(async (id: string) => (id === 'abc' ? detail() : pending));
    const cellCurves = vi.fn(async (_id: string, hashes: readonly string[]) => curves(hashes));
    const view = await renderResult({ get, cells: vi.fn(async () => page(leaderAndAnother())), cellCurves });
    await waitFor(() => expect(cellCurves.mock.calls).toEqual([['abc', ['h2']]]));

    view.fixture.componentRef.setInput('searchId', 'xyz');
    await view.fixture.whenStable();
    // The record on hand is still abc's: its leader is not asked of xyz.
    expect(cellCurves.mock.calls).toEqual([['abc', ['h2']]]);

    arrive(detail({ id: 'xyz' }));

    await waitFor(() => expect(cellCurves.mock.calls).toEqual([['abc', ['h2']], ['xyz', ['h2']]]));
    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('1 cell drawn'));
  });

  it('draws no more cells than the most it can hold', async () => {
    const three = [...leaderAndAnother(), cell({ params_hash: 'h3', params: { short_window: 20, long_window: 30 } })];
    await renderResult({ cells: vi.fn(async () => page(three)) }, { maxDrawn: 2 });
    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('1 cell drawn'));

    fireEvent.click(screen.getByRole('button', { name: 'Draw every cell on this page' }));

    await waitFor(() => expect(screen.getByText('2 of at most 2 drawn')).not.toBeNull());
    // The page held two more; only one fitted, and the other can no longer be ticked.
    expect((screen.getByRole('checkbox', { name: 'Draw short_window=20, long_window=30' }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Draw every cell on this page' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('draws every cell on the page that has a curve at one click', async () => {
    const cellCurves = vi.fn(async (_id: string, hashes: readonly string[]) => curves(hashes));
    await renderResult({ cells: vi.fn(async () => page([...leaderAndAnother(), cell({ params_hash: 'h3', params: { short_window: 20, long_window: 30 }, has_curve: false })])), cellCurves });
    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('1 cell drawn'));

    fireEvent.click(screen.getByRole('button', { name: 'Draw every cell on this page' }));

    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('2 cells drawn'));
    expect(cellCurves.mock.lastCall).toEqual(['abc', ['h1']]);
    expect((screen.getByRole('button', { name: 'Draw every cell on this page' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('lists the cells of a search that kept no curves, says so, and never draws one flat', async () => {
    const cellCurves = vi.fn(async (_id: string, hashes: readonly string[]): Promise<GridSearchCellCurves> => ({ sessions_ms: [], curves: [], not_recorded: [...hashes] }));
    const old = leaderAndAnother().map((item) => ({ ...item, has_curve: false }));
    await renderResult({ cells: vi.fn(async () => page(old, { without_curve: 2 })), cellCurves });

    expect(await screen.findByText('Completed cells with no curve recorded: 2. They are listed with their numbers and cannot be drawn.')).not.toBeNull();
    expect(screen.getAllByText('Curve not recorded')).toHaveLength(2);
    // No cell can be ticked, and the leader's row shows no tick for a curve that cannot be drawn.
    expect((screen.getAllByRole('checkbox') as HTMLInputElement[]).map((box) => [box.disabled, box.checked])).toEqual([[true, false], [true, false]]);
    // The owner did not tick it, so the panel names it.
    expect(await screen.findByText('No curve was recorded for the leader.')).not.toBeNull();
    expect(screen.getByText('0 of at most 200 drawn')).not.toBeNull();
    expect(within(screen.getByRole('region', { name: 'Equity curves' })).queryByRole('img')).toBeNull();
  });

  it('offers the curve read again when it fails', async () => {
    const cellCurves = vi.fn(async (): Promise<GridSearchCellCurves> => {
      throw new Error('offline');
    });
    await renderResult({ cells: vi.fn(async () => page(leaderAndAnother())), cellCurves });
    expect((await screen.findByRole('alert')).textContent).toContain('The curves could not be loaded.');

    cellCurves.mockImplementation(async () => curves(['h2']));
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    await waitFor(() => expect(drawnChart().getAttribute('aria-label')).toContain('1 cell drawn'));
  });

  it('re-fetches from the server when a column is sorted', async () => {
    const cells = vi.fn(async (_id: string, _query: CellPageQuery) => page([cell()]));
    await renderResult({ cells });
    await waitFor(() => expect(cells).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: /net profit/i }));


    await waitFor(() => expect(cells.mock.lastCall?.[1]).toMatchObject({ sort_by: 'net_profit', direction: 'desc', page: 1 }));
  });

  it('offers Cancel while running and requests it through the jobs boundary', async () => {
    const cancelJob = vi.fn(async () => undefined);
    const get = vi.fn(async (_id: string) => detail({ status: 'running', completed_cells: 1, expected_cells: 2 }));
    await render(GridSearchResultComponent, {
      inputs: { searchId: 'abc', pollMs: 0 },
      providers: [
        provideRouter([]),
        ...fakeCharts().providers,
        { provide: JobsService, useValue: { cancelJob } },
        { provide: GridSearchService, useValue: { get, cells: vi.fn(async () => page([cell()])), cellCurves: vi.fn(async (_id: string, hashes: readonly string[]) => curves(hashes)), delete: vi.fn(), finish: vi.fn() } },
      ],
    });

    await waitFor(() => expect(screen.getByRole('button', { name: /cancel/i })).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));

    await waitFor(() => expect(cancelJob).toHaveBeenCalledWith('job-1'));
  });

  it('says how many cells an incomplete search is missing', async () => {
    const get = vi.fn(async (_id: string) => detail({ status: 'cancelled', incomplete: true, completed_cells: 3, failed_cells: 1, expected_cells: 10 }));
    await renderResult({ get });

    await waitFor(() => expect(screen.getByText(/6 of 10 cells missing/)).not.toBeNull());
  });

  it('offers Finish only when the search is resumable, and explains why not otherwise', async () => {
    const get = vi.fn(async (_id: string) => detail({ status: 'interrupted', incomplete: true, resumable: false, resume_refusal: 'the engine or strategy code changed since launch; launch a fresh search' }));
    await renderResult({ get });

    await waitFor(() => expect(screen.getByText(/finish unavailable/i).textContent).toContain('code changed'));
    expect(screen.queryByRole('button', { name: /^finish$/i })).toBeNull();
  });

  it('deletes only after confirmation and emits the id', async () => {
    const del = vi.fn(async (_id: string) => undefined);
    const view = await renderResult({ delete: del });
    const deleted = vi.fn();
    view.fixture.componentInstance.deleted.subscribe(deleted);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(del).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: /confirm delete/i })).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /confirm delete/i }));

    await waitFor(() => expect(deleted).toHaveBeenCalledWith('abc'));
    expect(del).toHaveBeenCalledWith('abc');
  });
});
