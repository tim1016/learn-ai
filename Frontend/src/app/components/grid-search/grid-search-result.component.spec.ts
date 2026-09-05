import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GridSearchResultComponent } from './grid-search-result.component';
import { GridSearchService } from './grid-search.service';
import type { CellPageQuery, GridSearchCell, GridSearchCellPage, GridSearchDetail } from './grid-search.types';

function detail(overrides: Partial<GridSearchDetail> = {}): GridSearchDetail {
  return {
    id: 'abc',
    owner: { kind: 'user', owner_id: null, fold_index: null, phase: null },
    strategy_key: 'sma_crossover',
    symbol: 'SPY',
    status: 'completed',
    job_id: 'job-1',
    created_at_ms: 1704171600000,
    finished_at_ms: 1704172600000,
    window_start_ms: 1704171600000,
    window_end_ms: 1735621200000,
    measure: 'sharpe_ratio',
    min_trades: 5,
    expected_cells: 2,
    completed_cells: 2,
    failed_cells: 0,
    leader_params_hash: 'h2',
    leader_params: { short_window: 5, long_window: 30 },
    incomplete: false,
    uncommitted_changes: false,
    failure_reason: null,
    request: {} as never,
    receipt: {},
    resumable: false,
    resume_refusal: 'the search is complete',
    ...overrides,
  };
}

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
    is_leader: false,
    eligible: true,
    ...overrides,
  };
}

function page(cells: GridSearchCell[], overrides: Partial<GridSearchCellPage> = {}): GridSearchCellPage {
  return { total: cells.length, page: 1, page_size: 25, sort_by: 'sharpe_ratio', direction: 'desc', cells, ...overrides };
}

async function renderResult(service: Partial<GridSearchService>) {
  return render(GridSearchResultComponent, {
    inputs: { searchId: 'abc', pollMs: 0 },
    providers: [provideRouter([]), { provide: GridSearchService, useValue: { get: vi.fn(async (_id: string) => detail()), cells: vi.fn(async (_id: string, _query: CellPageQuery) => page([cell()])), delete: vi.fn(async (_id: string) => undefined), finish: vi.fn(async () => 'job-2'), ...service } }],
  });
}

describe('GridSearchResultComponent', () => {
  it('states that every figure is in-sample and routes into Walk-Forward with the search', async () => {
    await renderResult({});

    await waitFor(() => expect(screen.getByRole('note').textContent).toMatch(/in-sample/i));
    const link = screen.getByRole('link', { name: /walk-forward/i }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toContain('/walk-forward?search=abc');
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

  it('re-fetches from the server when a column is sorted', async () => {
    const cells = vi.fn(async (_id: string, _query: CellPageQuery) => page([cell()]));
    await renderResult({ cells });
    await waitFor(() => expect(cells).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: /net profit/i }));


    await waitFor(() => expect(cells.mock.lastCall?.[1]).toMatchObject({ sort_by: 'net_profit', direction: 'desc', page: 1 }));
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
