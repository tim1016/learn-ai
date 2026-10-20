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

  it('reads capabilities and defaults through the relative data-plane path the dev proxy guards, and asks the server to lay the dates out for the chosen months and for a picked range', async () => {
    const capabilities = service.capabilities();
    http.expectOne(`${BASE}/capabilities`).flush([]);
    await expect.soft(capabilities, 'reads capabilities through the relative data-plane path the dev proxy guards').resolves.toEqual([]);

    const defaults = service.defaults('ema_crossover_signal', 'SPY');
    const req = http.expectOne((r) => r.url === `${BASE}/defaults`);
    expect.soft(req.request.params.get('strategy_key'), 'reads the defaults of the strategy').toBe('ema_crossover_signal');
    expect.soft(req.request.params.get('symbol'), 'reads the defaults of the symbol').toBe('SPY');
    req.flush({ ...protocol(), incumbent_label: 'Registry', exposure: preflight().exposure });
    await expect.soft(defaults, 'resolves the defaults the server answered with').resolves.toMatchObject({ incumbent_label: 'Registry' });
    expect.soft(req.request.params.has('final_months'), 'sends no month counts when none are chosen').toBe(false);

    const byMonths = service.defaults('ema_crossover_signal', 'SPY', { final_months: 4, training_months: 9, test_months: 3 });
    const monthsReq = http.expectOne((r) => r.url === `${BASE}/defaults`);
    expect.soft(monthsReq.request.params.get('final_months'), 'asks the server to lay the dates out for the chosen final-test months').toBe('4');
    expect.soft(monthsReq.request.params.get('training_months'), 'asks the server to lay the dates out for the chosen training months').toBe('9');
    expect.soft(monthsReq.request.params.get('test_months'), 'asks the server to lay the dates out for the chosen test months').toBe('3');
    expect.soft(monthsReq.request.params.has('range_start_ms'), 'sends no range when none is picked').toBe(false);
    monthsReq.flush({ ...protocol(), incumbent_label: 'Registry', exposure: preflight().exposure });
    await expect.soft(byMonths, 'resolves the defaults laid out for the chosen months').resolves.toMatchObject({ incumbent_label: 'Registry' });

    const byRange = service.defaults('ema_crossover_signal', 'SPY', { final_months: 3, training_months: 6, test_months: 2, range_start_ms: 1726718400000, range_end_ms: 1789790400000 });
    const rangeReq = http.expectOne((r) => r.url === `${BASE}/defaults`);
    expect.soft(rangeReq.request.params.get('range_start_ms'), "sends a picked range's first day as its ET midnight").toBe('1726718400000');
    expect.soft(rangeReq.request.params.get('range_end_ms'), "sends a picked range's last day as its ET midnight").toBe('1789790400000');
    expect.soft(rangeReq.request.params.get('final_months'), 'sends the month counts with a picked range').toBe('3');
    rangeReq.flush({ ...protocol(), incumbent_label: 'Registry', exposure: preflight().exposure });
    await expect.soft(byRange, 'resolves the defaults laid out inside the picked range').resolves.toMatchObject({ incumbent_label: 'Registry' });
  });

  it('returns protocol refusals from preflight as data, not as an error', async () => {
    const pending = service.preflight(protocol());
    const req = http.expectOne(`${BASE}/preflight`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(protocol());
    req.flush(preflight({ refusals: [{ code: 'WORKLOAD_LIMIT', field: 'budget_cap', message: '6,200 runs exceed the cap of 5,000' }], estimate: null }));

    await expect(pending).resolves.toMatchObject({ refusals: [{ code: 'WORKLOAD_LIMIT' }] });
  });

  it('locks a plan with its idempotency key and starts no job when nothing was dispatched, and turns a refused lock into a GoldenSearchRefusedError', async () => {
    const pending = service.createStudy({ protocol: protocol(), idempotency_key: 'k-1' });
    const req = http.expectOne(`${BASE}/studies`);
    expect.soft(req.request.body, 'locks the plan with its idempotency key').toEqual({ protocol: protocol(), idempotency_key: 'k-1' });
    req.flush(studyDetail('locked'));

    await expect.soft(pending, 'resolves the locked study with no job').resolves.toMatchObject({ study: { state: 'locked' }, jobId: null });
    expect.soft(startJob, 'starts no job when nothing was dispatched').not.toHaveBeenCalled();

    const refused = service.createStudy({ protocol: protocol(), idempotency_key: 'k-1' });
    http.expectOne(`${BASE}/studies`).flush({ detail: { code: 'DATA_MISSING', message: 'the lake is missing 2 sessions' } }, { status: 400, statusText: 'Bad Request' });

    await expect.soft(refused, 'turns a refused lock into a GoldenSearchRefusedError').rejects.toBeInstanceOf(GoldenSearchRefusedError);
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

  it('reports a stale revision as a conflict that carries the study as it is now, and an idempotency conflict without a study', async () => {
    const stale = service.command('study-1', { command: 'continue', expected_revision: 2, idempotency_key: 'k-3', payload: {} });
    http
      .expectOne(`${BASE}/studies/study-1/commands`)
      .flush({ detail: { code: 'STALE_REVISION', message: 'the study moved on', study: studyDetail('awaiting_validation', { revision: 4 }) } }, { status: 409, statusText: 'Conflict' });

    const error = await stale.catch((e: unknown) => e);
    expect.soft(error, 'reports a stale revision as a StudyConflictError').toBeInstanceOf(StudyConflictError);
    expect.soft((error as StudyConflictError).code, 'names the stale revision in the conflict').toBe('STALE_REVISION');
    expect.soft((error as StudyConflictError).current?.revision, 'carries the study as it is now').toBe(4);

    const idempotency = service.command('study-1', { command: 'cancel', expected_revision: 2, idempotency_key: 'k-3', payload: {} });
    http.expectOne(`${BASE}/studies/study-1/commands`).flush({ detail: { code: 'IDEMPOTENCY_CONFLICT', message: 'that key was used for another request' } }, { status: 409, statusText: 'Conflict' });

    await expect.soft(idempotency, 'reports an idempotency conflict without a study').rejects.toMatchObject({ code: 'IDEMPOTENCY_CONFLICT', current: null });
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

  it('reads a page of runs by group, fold and order, a run’s curves by key, and a qualification deploy offer, each from its own path', async () => {
    const page = service.runs('study-1', { group: 'fold_test', fold_index: 0, sort: 'sharpe_ratio', descending: true, page: 2, page_size: 100 });
    const req = http.expectOne((r) => r.url === `${BASE}/studies/study-1/runs`);
    expect(['group', 'fold_index', 'sort', 'descending', 'page', 'page_size'].map((name) => req.request.params.get(name))).toEqual(['fold_test', '0', 'sharpe_ratio', 'true', '2', '100']);
    req.flush({ total: 0, page: 2, page_size: 100, sort: 'sharpe_ratio', folds: 2, groups: [], rows: [] });
    await page;
    // Every group and every fold: neither filter is sent.
    void service.runs('study-1', { group: null, fold_index: null, sort: 'recorded', descending: false, page: 1, page_size: 100 });
    const unfiltered = http.expectOne((r) => r.url === `${BASE}/studies/study-1/runs`);
    expect([unfiltered.request.params.has('group'), unfiltered.request.params.has('fold_index')]).toEqual([false, false]);
    unfiltered.flush({ total: 0, page: 1, page_size: 100, sort: 'recorded', folds: 2, groups: [], rows: [] });

    const curves = service.runCurves('study-1', ['k1', 'k2']);
    const read = http.expectOne((r) => r.url === `${BASE}/studies/study-1/runs/curves`);
    expect(read.request.params.getAll('key')).toEqual(['k1', 'k2']);
    read.flush({ curves: [], not_recorded: ['k1', 'k2'] });
    await expect(curves).resolves.toMatchObject({ not_recorded: ['k1', 'k2'] });

    const offer = service.deployOffer('q-1');
    http.expectOne('/api/research/golden-qualifications/q-1/deploy-offer').flush({ qualification_id: 'q-1', status: 'ready' });
    await expect(offer).resolves.toMatchObject({ status: 'ready' });
  });
});
