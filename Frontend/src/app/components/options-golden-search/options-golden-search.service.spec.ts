import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { OptionsGoldenSearchService, optionsSearchErrorMessage } from './options-golden-search.service';
import { study } from './testing';

const BASE = '/api/research/options-golden-search';

describe('OptionsGoldenSearchService command boundary', () => {
  let service: OptionsGoldenSearchService;
  let http: HttpTestingController;
  const startJob = vi.fn(async () => 'job-1');
  beforeEach(() => {
    startJob.mockReset().mockResolvedValue('job-1');
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), { provide: JobsService, useValue: { startJob } }] });
    service = TestBed.inject(OptionsGoldenSearchService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());


  it('reads one exact session through the scoped candidate and window endpoint', async () => {
    const pending = service.session('study/one', 'c'.repeat(64), 'final', 1791294000000);
    const request = http.expectOne((item) => item.url === `${BASE}/studies/study%2Fone/candidates/${'c'.repeat(64)}/sessions/1791294000000`);
    expect(request.request.params.get('window')).toBe('final');
    const session = { session_open_ms: 1791294000000, net_profit_usd: '0', status: 'no_trade', detail: 'No opening fill.', attempts: [] };
    request.flush(session);
    await expect(pending).resolves.toEqual(session);
  });

  it('dispatches only the exact stage token returned by the committed command', async () => {
    const request = { command: 'run_research', expected_revision: 4, idempotency_key: 'intent-1' } as const;
    const pending = service.command('study/one', request);
    const sent = http.expectOne(`${BASE}/studies/study%2Fone/commands`);
    expect(sent.request.body).toEqual(request);
    sent.flush(study({ dispatch: { study_id: 'study/one', stage_token: 'fenced-token' } }));
    await pending;
    expect(startJob).toHaveBeenCalledExactlyOnceWith('options_golden_search', { study_id: 'study/one', stage_token: 'fenced-token' });
  });

  it('preserves the committed study when job dispatch fails so it can be resumed', async () => {
    startJob.mockRejectedValue(new Error('Job service unavailable'));
    const committed = study({ dispatch: { study_id: 'study-1', stage_token: 'reserved-stage' } });
    const pending = service.command('study-1', { command: 'resume', expected_revision: 4, idempotency_key: 'intent-2' });
    const assertion = expect(pending).rejects.toMatchObject({ study: committed });
    http.expectOne(`${BASE}/studies/study-1/commands`).flush(committed);
    await assertion;
  });

  it('does not dispatch after a stale revision refusal', async () => {
    const pending = service.command('study-1', { command: 'run_research', expected_revision: 3, idempotency_key: 'intent-3' });
    const assertion = expect(pending).rejects.toBeInstanceOf(HttpErrorResponse);
    http.expectOne(`${BASE}/studies/study-1/commands`).flush({ detail: 'Stale revision' }, { status: 409, statusText: 'Conflict' });
    await assertion;
    expect(startJob).not.toHaveBeenCalled();
  });
});

it('preserves rejected plan field paths and backend validation reasons', () => {
  const error = new HttpErrorResponse({ status: 422, error: { detail: [{ loc: ['body', 'knobs', 0, 'values'], msg: 'Candidate values must be distinct' }] } });
  expect(optionsSearchErrorMessage(error, 'Plan rejected')).toBe('body.knobs.0.values: Candidate values must be distinct');
});
