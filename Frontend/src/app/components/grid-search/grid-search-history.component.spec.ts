import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GridSearchHistoryComponent } from './grid-search-history.component';
import { GridSearchService } from './grid-search.service';
import type { GridSearchSummary } from './grid-search.types';

function row(overrides: Partial<GridSearchSummary> = {}): GridSearchSummary {
  return {
    id: 'abc',
    owner: { kind: 'user', owner_id: null, fold_index: null, phase: null },
    strategy_key: 'sma_crossover',
    symbol: 'SPY',
    status: 'running',
    job_id: 'job-1',
    created_at_ms: 1704171600000,
    finished_at_ms: null,
    window_start_ms: 1704171600000,
    window_end_ms: 1735621200000,
    measure: 'sharpe_ratio',
    min_trades: 5,
    expected_cells: 4,
    completed_cells: 1,
    failed_cells: 0,
    leader_params_hash: null,
    leader_params: null,
    incomplete: false,
    uncommitted_changes: true,
    failure_reason: null,
    ...overrides,
  };
}

describe('GridSearchHistoryComponent', () => {
  it('renders each row with strategy, symbol, cells, status, and the uncommitted label', async () => {
    const list = vi.fn(async () => [row()]);
    await render(GridSearchHistoryComponent, {
      inputs: { strategies: [{ name: 'sma_crossover', display_name: 'SMA Crossover' } as never] },
      providers: [{ provide: GridSearchService, useValue: { list, delete: vi.fn() } }],
    });

    const table = await waitFor(() => screen.getByRole('table'));
    expect(within(table).getByText('SMA Crossover')).not.toBeNull();
    expect(within(table).getByText('SPY')).not.toBeNull();
    expect(within(table).getByText('1 / 4')).not.toBeNull();
    expect(within(table).getByText('Running')).not.toBeNull();
    expect(within(table).getByText('Uncommitted changes')).not.toBeNull();
  });

  it('narrows the list through the filters', async () => {
    const list = vi.fn(async () => []);
    await render(GridSearchHistoryComponent, { providers: [{ provide: GridSearchService, useValue: { list, delete: vi.fn() } }] });
    await waitFor(() => expect(list).toHaveBeenCalledWith({}));

    fireEvent.change(screen.getByRole('combobox', { name: /filter by status/i }), { target: { value: 'completed' } });

    await waitFor(() => expect(list).toHaveBeenLastCalledWith({ status: 'completed' }));
  });

  it('opens a row and deletes only after confirmation', async () => {
    const del = vi.fn(async () => undefined);
    const view = await render(GridSearchHistoryComponent, {
      providers: [{ provide: GridSearchService, useValue: { list: vi.fn(async () => [row({ status: 'completed' })]), delete: del } }],
    });
    const opened = vi.fn();
    view.fixture.componentInstance.opened.subscribe(opened);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Open search abc' })).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Open search abc' }));
    expect(opened).toHaveBeenCalledWith('abc');

    fireEvent.click(screen.getByRole('button', { name: 'Delete search abc' }));
    expect(del).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: /confirm delete/i })).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /confirm delete/i }));

    await waitFor(() => expect(del).toHaveBeenCalledWith('abc'));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Open search abc' })).toBeNull());
  });
});
