import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import { RecencyChartService } from './recency-chart.service';

const BASE = `${environment.pythonServiceUrl}/api/research/recency`;

describe('RecencyChartService', () => {
  let service: RecencyChartService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(RecencyChartService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('sends the window and repeats every symbol and strategy as its own query key', async () => {
    const pending = firstValueFrom(service.trades({ fromMs: 10, toMs: 20, symbols: ['SPY', 'QQQ'], strategies: ['sma_crossover'] }));
    const req = http.expectOne((r) => r.url === `${BASE}/trades`);

    expect(req.request.method).toBe('GET');
    expect(req.request.params.getAll('symbols')).toEqual(['SPY', 'QQQ']);
    expect(req.request.params.getAll('strategies')).toEqual(['sma_crossover']);
    expect(req.request.params.get('from_ms')).toBe('10');
    expect(req.request.params.get('to_ms')).toBe('20');
    req.flush([]);
    await expect(pending).resolves.toEqual([]);
  });

  it('omits empty filters so the server reads "any"', async () => {
    const pending = firstValueFrom(service.heroes({ fromMs: 1, toMs: 2, symbols: [], strategies: [] }));
    const req = http.expectOne((r) => r.url === `${BASE}/hero`);

    expect(req.request.params.has('symbols')).toBe(false);
    expect(req.request.params.has('strategies')).toBe(false);
    req.flush({ heroes: [{ recency_run_id: 7, symbol: 'SPY', strategy_key: 'sma_crossover', params_hash: 'h1', total_pnl: 12.5 }] });
    await expect(pending).resolves.toEqual([{ recencyRunId: 7, symbol: 'SPY', strategyKey: 'sma_crossover', paramsHash: 'h1', totalPnl: 12.5 }]);
  });

  it('maps the snake_case trade DTO, memberships included, onto the chart model', async () => {
    const pending = firstValueFrom(service.trades({ fromMs: 0, toMs: 1 }));
    http.expectOne((r) => r.url === `${BASE}/trades`).flush([
      {
        symbol: 'SPY',
        strategy_key: 'sma_crossover',
        params_hash: 'h1',
        params_json: '{"short_window": 2.0}',
        fingerprint: 'fp1',
        entry_ms: 1000,
        exit_ms: 2000,
        pnl_pts: 2,
        pnl_pct: 0.02,
        quantity: 10,
        pnl: 20,
        holding_sessions: 1,
        sharpe: null,
        study_id: 42,
        recency_run_id: 7,
        is_synthetic_exit: true,
        signal_reason: 'exit_at_close',
        memberships: [{ recency_run_id: 7, study_id: 42, created_at_ms: 900 }, { recency_run_id: 3, study_id: null, created_at_ms: 800 }],
      },
    ]);

    await expect(pending).resolves.toEqual([
      {
        symbol: 'SPY',
        strategyKey: 'sma_crossover',
        paramsHash: 'h1',
        paramsJson: '{"short_window": 2.0}',
        fingerprint: 'fp1',
        entryMs: 1000,
        exitMs: 2000,
        pnlPts: 2,
        pnlPct: 0.02,
        quantity: 10,
        pnl: 20,
        holdingSessions: 1,
        sharpe: null,
        studyId: 42,
        recencyRunId: 7,
        isSyntheticExit: true,
        signalReason: 'exit_at_close',
        memberships: [
          { recencyRunId: 7, studyId: 42, createdAtMs: 900 },
          { recencyRunId: 3, studyId: null, createdAtMs: 800 },
        ],
      },
    ]);
  });

  it('soft-deletes a run through its verb and rejects when the run is unknown', async () => {
    const ok = service.softDeleteRun(7);
    const req = http.expectOne(`${BASE}/runs/7/soft-delete`);
    expect(req.request.method).toBe('POST');
    req.flush({ recency_run_id: 7 });
    await expect(ok).resolves.toBeUndefined();

    const missing = service.softDeleteRun(99);
    http.expectOne(`${BASE}/runs/99/soft-delete`).flush({ detail: { code: 'RECENCY_RUN_NOT_FOUND', message: 'RecencyRun 99 not found' } }, { status: 404, statusText: 'Not Found' });
    await expect(missing).rejects.toBeTruthy();
  });
});
