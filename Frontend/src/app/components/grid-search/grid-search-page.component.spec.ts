import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GridSearchPageComponent } from './grid-search-page.component';
import { GridSearchService } from './grid-search.service';

describe('GridSearchPageComponent', () => {
  it('renders the two tabs and opens a launched search by its job id', async () => {
    const list = vi.fn(async () => [{ id: 'new-1', status: 'queued' }]);
    const view = await render(GridSearchPageComponent, {
      providers: [
        provideRouter([]),
        {
          provide: GridSearchService,
          useValue: {
            loadStrategies: vi.fn(async () => []),
            list,
            get: vi.fn(async () => ({ id: 'new-1', status: 'queued', strategy_key: 'sma_crossover', symbol: 'SPY', window_start_ms: 0, window_end_ms: 1, measure: 'sharpe_ratio', min_trades: 5, created_at_ms: 0, completed_cells: 0, failed_cells: 0, expected_cells: 1, leader_params: null, incomplete: false, uncommitted_changes: false, failure_reason: null, resumable: false, resume_refusal: null })),
            cells: vi.fn(async () => ({ total: 0, page: 1, page_size: 25, sort_by: 'sharpe_ratio', direction: 'desc', cells: [] })),
          },
        },
      ],
    });

    expect(screen.getByRole('tab', { name: /new search/i })).not.toBeNull();
    expect(screen.getByRole('tab', { name: /history/i })).not.toBeNull();

    await view.fixture.componentInstance.onLaunched({ jobId: 'job-7' });
    await view.fixture.whenStable();

    expect(list).toHaveBeenCalledWith({ job_id: 'job-7' });
    await waitFor(() => expect(view.fixture.componentInstance.openSearchId()).toBe('new-1'));
    expect(view.fixture.componentInstance.activeTab()).toBe('history');
  });
});
