import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { OptionsBacktestService, RunRefusedError } from './options-backtest.service';
import { SERVED_LIMITS, exampleBody } from './testing/fixtures';

const BASE = '/api/research/options-backtest';
const REFUSAL = { code: 'OPTIONS_BACKTEST_RANGE_TOO_LONG', message: 'That range holds more sessions than a run takes.' };

describe('OptionsBacktestService, the run routes', () => {
  let service: OptionsBacktestService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), { provide: JobsService, useValue: {} }] });
    service = TestBed.inject(OptionsBacktestService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('reads the limits from the relative route', async () => {
    const pending = service.limits();
    http.expectOne(`${BASE}/limits`).flush(SERVED_LIMITS);

    await expect(pending).resolves.toEqual(SERVED_LIMITS);
  });

  it('posts the setup and the two instants to check data', async () => {
    const pending = service.checkData(exampleBody(), 1_000, 2_000);
    const request = http.expectOne(`${BASE}/check-data`);
    request.flush({ option_prices_from_ms: 5, sessions: [], counts: { ready: 0, needs_download: 0, cannot_be_tested: 0 } });

    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ setup: exampleBody(), start_ms: 1_000, end_ms: 2_000 });
    await expect(pending).resolves.toMatchObject({ option_prices_from_ms: 5 });
  });

  it.each([
    [400, 'a range the server refuses'],
    [503, 'a catalog it cannot ask'],
  ])('reads a %s on check data as a refusal with the server’s code and message: %s', async (status) => {
    const pending = service.checkData(exampleBody(), 1_000, 2_000);
    http.expectOne(`${BASE}/check-data`).flush({ detail: REFUSAL }, { status, statusText: 'x' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(RunRefusedError);
    expect(error).toMatchObject({ code: REFUSAL.code, message: REFUSAL.message, status });
  });

  it('lists the runs, only the one job’s when asked, and only as many as asked for', async () => {
    const all = service.runs();
    http.expectOne((request) => request.url === `${BASE}/runs` && request.params.keys().length === 0).flush([]);
    await all;

    const one = service.runs({ jobId: 'job-7', limit: 5 });
    const request = http.expectOne((candidate) => candidate.url === `${BASE}/runs`);
    request.flush([]);

    expect(request.request.params.get('job_id')).toBe('job-7');
    expect(request.request.params.get('limit')).toBe('5');
    await one;
  });

  it('reads one run by its id, and a 404 as a refusal', async () => {
    const found = service.run(12);
    http.expectOne(`${BASE}/runs/12`).flush({ id: 12 });
    await expect(found).resolves.toMatchObject({ id: 12 });

    const missing = service.run(13);
    http.expectOne(`${BASE}/runs/13`).flush({ detail: { code: 'OPTIONS_BACKTEST_RUN_NOT_FOUND', message: 'No such run.' } }, { status: 404, statusText: 'Not Found' });
    await expect(missing).rejects.toMatchObject({ code: 'OPTIONS_BACKTEST_RUN_NOT_FOUND', status: 404 });
  });

  it('deletes a run by its id, and reads a 409 as the refusal of a run that is going', async () => {
    const gone = service.deleteRun(12);
    const request = http.expectOne(`${BASE}/runs/12`);
    request.flush(null, { status: 204, statusText: 'No Content' });
    expect(request.request.method).toBe('DELETE');
    await expect(gone).resolves.toBeUndefined();

    const going = service.deleteRun(14);
    http.expectOne(`${BASE}/runs/14`).flush({ detail: { code: 'OPTIONS_BACKTEST_RUN_IS_RUNNING', message: 'That run is still going.' } }, { status: 409, statusText: 'Conflict' });
    const error = await going.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(RunRefusedError);
    expect(error).toMatchObject({ message: 'That run is still going.', status: 409 });
  });

  it('leaves an error that is no refusal as it was', async () => {
    const pending = service.run(15);
    http.expectOne(`${BASE}/runs/15`).flush('boom', { status: 500, statusText: 'Server Error' });

    await expect(pending).rejects.toBeInstanceOf(HttpErrorResponse);
  });
});

describe('starting a run', () => {
  let service: OptionsBacktestService;
  let startJob: ReturnType<typeof vi.fn<(type: string, payload: Record<string, unknown>) => Promise<string>>>;

  function serviceWith(job: (type: string, payload: Record<string, unknown>) => Promise<string>) {
    startJob = vi.fn(job);
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), { provide: JobsService, useValue: { startJob } }] });
    service = TestBed.inject(OptionsBacktestService);
  }
  const request = { setup: exampleBody(), start_ms: 1_000, end_ms: 2_000, setup_name: 'Mine' };

  it('starts an options_backtest job with the setup, the range and the name, and hands back the job id', async () => {
    serviceWith(async () => 'job-7');

    await expect(service.startRun(request)).resolves.toBe('job-7');

    expect(startJob).toHaveBeenCalledWith('options_backtest', request);
  });

  it('reads a refusal the data plane sent straight, with the server’s message', async () => {
    serviceWith(async () => {
      throw new HttpErrorResponse({ status: 400, error: { detail: REFUSAL } });
    });

    const error = await service.startRun(request).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(RunRefusedError);
    expect(error).toMatchObject({ code: REFUSAL.code, message: REFUSAL.message });
  });

  it('reads a refusal the jobs boundary forwarded as the text of a detail', async () => {
    serviceWith(async () => {
      throw new HttpErrorResponse({ status: 400, error: { detail: JSON.stringify({ detail: REFUSAL }) } });
    });

    const error = await service.startRun(request).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(RunRefusedError);
    expect(error).toMatchObject({ code: REFUSAL.code, message: REFUSAL.message });
  });

  it('leaves a failure that is no refusal as it was', async () => {
    serviceWith(async () => {
      throw new HttpErrorResponse({ status: 502, error: 'bad gateway' });
    });

    await expect(service.startRun(request)).rejects.toBeInstanceOf(HttpErrorResponse);
  });
});
