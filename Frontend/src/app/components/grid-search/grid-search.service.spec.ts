import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { environment } from '../../../environments/environment';
import { JobsService } from '../../services/jobs.service';
import { GridSearchRefusedError, GridSearchService } from './grid-search.service';
import type { GridSearchSpecRequest } from './grid-search.types';

const BASE = `${environment.pythonServiceUrl}/api/research/grid-search`;

const SPEC: GridSearchSpecRequest = {
  strategy_key: 'sma_crossover',
  symbol: 'SPY',
  param_ranges: { short_window: { type: 'value_list', values: [5, 10] } },
  start_ms: 1704171600000,
  end_ms: 1735621200000,
  resolution: 'minute',
  fill_mode: 'signal_bar_close',
  commission_per_order: 1,
  slippage_per_share: 0,
  initial_cash: 100000,
  measure: 'sharpe_ratio',
  min_trades: 5,
};

describe('GridSearchService', () => {
  let service: GridSearchService;
  let http: HttpTestingController;
  const startJob = vi.fn(async () => 'job-1');

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), { provide: JobsService, useValue: { startJob } }],
    });
    service = TestBed.inject(GridSearchService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('posts the spec to preflight and returns the plan', async () => {
    const pending = service.preflight(SPEC);
    const req = http.expectOne(`${BASE}/preflight`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(SPEC);
    req.flush({ strategy_key: 'sma_crossover', symbol: 'SPY', combinations: 2, total_backtests: 2, backtest_limit: 5000, estimated_seconds: 4, expected_sessions: 250, run_up: {} });

    await expect(pending).resolves.toMatchObject({ combinations: 2 });
  });

  it('turns a 400 with a code into a GridSearchRefusedError', async () => {
    const pending = service.preflight(SPEC);
    http.expectOne(`${BASE}/preflight`).flush({ detail: { code: 'DATA_MISSING', message: 'the lake is missing 2 sessions' } }, { status: 400, statusText: 'Bad Request' });

    await expect(pending).rejects.toBeInstanceOf(GridSearchRefusedError);
    await pending.catch((error: GridSearchRefusedError) => expect(error.refusal.code).toBe('DATA_MISSING'));
  });

  it('launches through the jobs boundary as a grid_search job', async () => {
    await expect(service.launch(SPEC)).resolves.toBe('job-1');
    expect(startJob).toHaveBeenCalledWith('grid_search', SPEC);
  });

  it('finishes an incomplete search by resubmitting its stored request with resume_search_id', async () => {
    await service.finish({ id: 'abc', request: SPEC } as never);
    expect(startJob).toHaveBeenLastCalledWith('grid_search', { ...SPEC, resume_search_id: 'abc' });
  });

  it('lists with only the filters that are set', async () => {
    const pending = service.list({ strategy_key: 'sma_crossover', symbol: '', status: undefined, job_id: 'job-1' });
    const req = http.expectOne((r) => r.url === BASE);
    expect(req.request.params.keys().sort()).toEqual(['job_id', 'strategy_key']);
    req.flush([]);
    await expect(pending).resolves.toEqual([]);
  });

  it('requests a sorted, paged slice of cells', async () => {
    const pending = service.cells('abc', { sort_by: 'net_profit', direction: 'asc', page: 2, page_size: 25 });
    const req = http.expectOne((r) => r.url === `${BASE}/abc/cells`);
    expect(req.request.params.get('sort_by')).toBe('net_profit');
    expect(req.request.params.get('page')).toBe('2');
    req.flush({ total: 0, page: 2, page_size: 25, sort_by: 'net_profit', direction: 'asc', cells: [] });
    await expect(pending).resolves.toMatchObject({ total: 0 });
  });

  it('deletes by id', async () => {
    const pending = service.delete('abc');
    const req = http.expectOne(`${BASE}/abc`);
    expect(req.request.method).toBe('DELETE');
    req.flush(null, { status: 204, statusText: 'No Content' });
    await expect(pending).resolves.toBeUndefined();
  });
});
