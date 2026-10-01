import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { GoldenSearchRefusedError, GoldenSearchService, StageDispatchError, StudyConflictError } from './golden-search.service';
import type { StudyCommandRequest } from './golden-search.types';
import { preflight, protocol, studyDetail } from './testing/fixtures';

const BASE = '/api/research/golden-search';

describe('GoldenSearchService', () => {
  let service: GoldenSearchService;
  let http: HttpTestingController;
  let startJob: ReturnType<typeof vi.fn<(type: string, payload: Record<string, unknown>) => Promise<string>>>;

  beforeEach(() => {
    startJob = vi.fn(async (_type: string, _payload: Record<string, unknown>) => 'job-9');
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), { provide: JobsService, useValue: { startJob } }],
    });
    service = TestBed.inject(GoldenSearchService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('reads capabilities and defaults through the relative data-plane path the dev proxy guards', async () => {
    const capabilities = service.capabilities();
    http.expectOne(`${BASE}/capabilities`).flush([]);
    await expect(capabilities).resolves.toEqual([]);

    const defaults = service.defaults('ema_crossover_signal', 'SPY');
    const req = http.expectOne((r) => r.url === `${BASE}/defaults`);
    expect(req.request.params.get('strategy_key')).toBe('ema_crossover_signal');
    expect(req.request.params.get('symbol')).toBe('SPY');
    req.flush({ ...protocol(), incumbent_label: 'Registry', exposure: preflight().exposure });
    await expect(defaults).resolves.toMatchObject({ incumbent_label: 'Registry' });
  });

  it('returns protocol refusals from preflight as data, not as an error', async () => {
    const pending = service.preflight(protocol());
    const req = http.expectOne(`${BASE}/preflight`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(protocol());
    req.flush(preflight({ refusals: [{ code: 'WORKLOAD_LIMIT', field: 'budget_cap', message: '6,200 runs exceed the cap of 5,000' }], estimate: null }));

    await expect(pending).resolves.toMatchObject({ refusals: [{ code: 'WORKLOAD_LIMIT' }] });
  });

  it('locks a plan with its idempotency key and starts no job when nothing was dispatched', async () => {
    const pending = service.createStudy({ protocol: protocol(), idempotency_key: 'k-1' });
    const req = http.expectOne(`${BASE}/studies`);
    expect(req.request.body).toEqual({ protocol: protocol(), idempotency_key: 'k-1' });
    req.flush(studyDetail('locked'));

    await expect(pending).resolves.toMatchObject({ study: { state: 'locked' }, jobId: null });
    expect(startJob).not.toHaveBeenCalled();
  });

  it('turns a refused lock into a GoldenSearchRefusedError', async () => {
    const pending = service.createStudy({ protocol: protocol(), idempotency_key: 'k-1' });
    http.expectOne(`${BASE}/studies`).flush({ detail: { code: 'DATA_MISSING', message: 'the lake is missing 2 sessions' } }, { status: 400, statusText: 'Bad Request' });

    await expect(pending).rejects.toBeInstanceOf(GoldenSearchRefusedError);
  });

  it('sends a command envelope and starts the stage it authorized as a golden_search job carrying the stage token', async () => {
    const request: StudyCommandRequest = { command: 'continue', expected_revision: 3, idempotency_key: 'k-2', payload: {} };
    const pending = service.command('study-1', request);
    const req = http.expectOne(`${BASE}/studies/study-1/commands`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(request);
    req.flush(studyDetail('search_running', { dispatch: { job_type: 'golden_search', payload: { study_id: 'study-1', stage_token: 'tok-7' } } }));

    await expect(pending).resolves.toMatchObject({ jobId: 'job-9', study: { state: 'search_running' } });
    expect(startJob).toHaveBeenCalledWith('golden_search', { study_id: 'study-1', stage_token: 'tok-7' });
  });

  it('reports a stale revision as a conflict that carries the study as it is now', async () => {
    const pending = service.command('study-1', { command: 'continue', expected_revision: 2, idempotency_key: 'k-3', payload: {} });
    http
      .expectOne(`${BASE}/studies/study-1/commands`)
      .flush({ detail: { code: 'STALE_REVISION', message: 'the study moved on', study: studyDetail('awaiting_validation', { revision: 4 }) } }, { status: 409, statusText: 'Conflict' });

    const error = await pending.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(StudyConflictError);
    expect((error as StudyConflictError).code).toBe('STALE_REVISION');
    expect((error as StudyConflictError).current?.revision).toBe(4);
  });

  it('reports an idempotency conflict without a study', async () => {
    const pending = service.command('study-1', { command: 'cancel', expected_revision: 2, idempotency_key: 'k-3', payload: {} });
    http.expectOne(`${BASE}/studies/study-1/commands`).flush({ detail: { code: 'IDEMPOTENCY_CONFLICT', message: 'that key was used for another request' } }, { status: 409, statusText: 'Conflict' });

    await expect(pending).rejects.toMatchObject({ code: 'IDEMPOTENCY_CONFLICT', current: null });
  });

  it('keeps the committed study when its job could not be started', async () => {
    startJob.mockRejectedValueOnce(new HttpErrorResponse({ status: 502 }));
    const pending = service.command('study-1', { command: 'finish', expected_revision: 5, idempotency_key: 'k-4', payload: {} });
    http.expectOne(`${BASE}/studies/study-1/commands`).flush(studyDetail('search_running', { dispatch: { job_type: 'golden_search', payload: { study_id: 'study-1', stage_token: 'tok-8' } } }));

    const error = await pending.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(StageDispatchError);
    expect((error as StageDispatchError).study.state).toBe('search_running');
  });

  it('lists with only the filters that are set and hides by DELETE', async () => {
    const list = service.list({ strategy_key: 'ema_crossover_signal', symbol: '', include_hidden: false, limit: 50 });
    const req = http.expectOne((r) => r.url === `${BASE}/studies`);
    expect(req.request.params.keys().sort()).toEqual(['limit', 'strategy_key']);
    req.flush([]);
    await list;

    const withHidden = service.list({ include_hidden: true });
    const hiddenReq = http.expectOne((r) => r.url === `${BASE}/studies`);
    expect(hiddenReq.request.params.get('include_hidden')).toBe('true');
    hiddenReq.flush([]);
    await withHidden;

    const hide = service.hide('study-1');
    const del = http.expectOne(`${BASE}/studies/study-1`);
    expect(del.request.method).toBe('DELETE');
    del.flush(null, { status: 204, statusText: 'No Content' });
    await expect(hide).resolves.toBeUndefined();
  });

  it('reads paged evaluations and a qualification deploy offer from their own paths', async () => {
    const page = service.evaluations('study-1', { stage: 'validation', fold_index: 0, page: 2, page_size: 50 });
    const req = http.expectOne((r) => r.url === `${BASE}/studies/study-1/evaluations`);
    expect(req.request.params.get('stage')).toBe('validation');
    expect(req.request.params.get('fold_index')).toBe('0');
    expect(req.request.params.get('page')).toBe('2');
    req.flush({ total: 0, page: 2, page_size: 50, rows: [] });
    await page;

    const offer = service.deployOffer('q-1');
    http.expectOne('/api/research/golden-qualifications/q-1/deploy-offer').flush({ qualification_id: 'q-1', status: 'ready' });
    await expect(offer).resolves.toMatchObject({ status: 'ready' });
  });
});
