import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { filenameOf, RunDetailRefusedError, RunDetailService } from './run-detail.service';
import { tradesPage, TUESDAY_OPEN } from './testing/detail-fixtures';
import { ensureBlobText, textOf } from './testing/blob-text';

const BASE = '/api/research/options-backtest';
const REFUSAL = { code: 'OPTIONS_BACKTEST_RUN_NOT_COMPLETE', message: 'Run 7 is running. Only a complete run has trades and sessions.' };

describe('RunDetailService, the trades route', () => {
  let service: RunDetailService;
  let http: HttpTestingController;

  beforeEach(() => {
    ensureBlobText();
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(RunDetailService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('reads a run’s trades from the relative route and sends only the filters that are set', async () => {
    const pending = service.trades(7);
    const request = http.expectOne((req) => req.url === `${BASE}/runs/7/trades`);
    request.flush(tradesPage());

    expect(request.request.method).toBe('GET');
    expect(request.request.params.keys()).toEqual([]);
    await expect(pending).resolves.toEqual(tradesPage());
  });

  it('sends the session, the leg, the limit and the offset it was asked for, as the server names them', async () => {
    const pending = service.trades(7, { sessionOpenMs: TUESDAY_OPEN, legId: 'call', limit: 100, offset: 200 });
    const request = http.expectOne((req) => req.url === `${BASE}/runs/7/trades`);
    request.flush(tradesPage());
    await pending;

    expect(request.request.params.get('session_open_ms')).toBe(String(TUESDAY_OPEN));
    expect(request.request.params.get('leg_id')).toBe('call');
    expect(request.request.params.get('limit')).toBe('100');
    expect(request.request.params.get('offset')).toBe('200');
  });

  it('asks for the first page with an offset of 0, which is a filter that is set', async () => {
    const pending = service.trades(7, { offset: 0 });
    const request = http.expectOne((req) => req.url === `${BASE}/runs/7/trades`);
    request.flush(tradesPage());
    await pending;

    expect(request.request.params.get('offset')).toBe('0');
  });

  it('does not send a leg that is empty or a session that is null', async () => {
    const pending = service.trades(7, { sessionOpenMs: null, legId: '' });
    const request = http.expectOne((req) => req.url === `${BASE}/runs/7/trades`);
    request.flush(tradesPage());
    await pending;

    expect(request.request.params.keys()).toEqual([]);
  });

  it.each([
    [404, 'OPTIONS_BACKTEST_SESSION_NOT_IN_RUN', 'Run 7 did not test a session that opened at 5.'],
    [404, 'OPTIONS_BACKTEST_LEG_NOT_IN_RUN', "Run 7 has no leg 'wing'."],
    [409, 'OPTIONS_BACKTEST_RUN_NOT_COMPLETE', REFUSAL.message],
  ])('reads a %s (%s) as a refusal with the server’s code and message', async (status, code, message) => {
    const pending = service.trades(7);
    http.expectOne((req) => req.url === `${BASE}/runs/7/trades`).flush({ detail: { code, message } }, { status, statusText: 'x' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(RunDetailRefusedError);
    expect(error).toMatchObject({ code, message, status });
  });

  it('lets a failure that is not a refusal pass through as it came', async () => {
    const pending = service.trades(7);
    http.expectOne((req) => req.url === `${BASE}/runs/7/trades`).flush('down', { status: 503, statusText: 'Service Unavailable' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(HttpErrorResponse);
    expect(error).not.toBeInstanceOf(RunDetailRefusedError);
  });
});

describe('RunDetailService, one session', () => {
  let service: RunDetailService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(RunDetailService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('reads the session by the instant of its open', async () => {
    const pending = service.session(7, TUESDAY_OPEN);
    const request = http.expectOne(`${BASE}/runs/7/sessions/${TUESDAY_OPEN}`);
    request.flush({ run_id: 7 });

    expect(request.request.method).toBe('GET');
    await expect(pending).resolves.toEqual({ run_id: 7 });
  });

  it.each([
    [404, 'OPTIONS_BACKTEST_SESSION_NOT_IN_RUN', 'Run 7 did not test a session that opened at 5.'],
    [409, 'OPTIONS_BACKTEST_RUN_NOT_COMPLETE', REFUSAL.message],
    [500, 'OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT', 'The stored trades and events of this session do not agree, so it cannot be drawn: leg call.'],
  ])('reads a %s (%s) as a refusal with the server’s code and message', async (status, code, message) => {
    const pending = service.session(7, TUESDAY_OPEN);
    http.expectOne(`${BASE}/runs/7/sessions/${TUESDAY_OPEN}`).flush({ detail: { code, message } }, { status, statusText: 'x' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(RunDetailRefusedError);
    expect(error).toMatchObject({ code, message, status });
  });
});

describe('RunDetailService, the export', () => {
  let service: RunDetailService;
  let http: HttpTestingController;

  beforeEach(() => {
    ensureBlobText();
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(RunDetailService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('hands back the bytes the server sent, untouched, and the file name it gave them', async () => {
    const pending = service.tradesCsv(7);
    const request = http.expectOne(`${BASE}/runs/7/trades.csv`);
    request.flush(new Blob(['unix_ts,time_america_new_york\r\n1,2\r\n'], { type: 'text/csv' }), {
      headers: { 'Content-Disposition': 'attachment; filename="options-backtest-run-7-trades.csv"' },
    });

    const file = await pending;
    expect(request.request.responseType).toBe('blob');
    expect(file.filename).toBe('options-backtest-run-7-trades.csv');
    expect(await textOf(file.blob)).toBe('unix_ts,time_america_new_york\r\n1,2\r\n');
  });

  it('gives no name when the server named none, and the caller then chooses one', async () => {
    const pending = service.tradesCsv(7);
    http.expectOne(`${BASE}/runs/7/trades.csv`).flush(new Blob(['x']));

    expect((await pending).filename).toBeNull();
  });

  it.each([
    [409, 'OPTIONS_BACKTEST_RUN_NOT_COMPLETE', REFUSAL.message],
    [404, 'OPTIONS_BACKTEST_RUN_NOT_FOUND', 'No run has the id 7.'],
  ])('reads the server’s words from a %s that came as a Blob (%s)', async (status, code, message) => {
    const pending = service.tradesCsv(7);
    http.expectOne(`${BASE}/runs/7/trades.csv`).flush(new Blob([JSON.stringify({ detail: { code, message } })], { type: 'application/json' }), { status, statusText: 'x' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(RunDetailRefusedError);
    expect(error).toMatchObject({ code, message, status });
  });

  it('lets a failure whose body is not the server’s JSON pass through as it came', async () => {
    const pending = service.tradesCsv(7);
    http.expectOne(`${BASE}/runs/7/trades.csv`).flush(new Blob(['<html>bad gateway</html>']), { status: 409, statusText: 'x' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(HttpErrorResponse);
  });
});

describe('the file name in a Content-Disposition header', () => {
  it.each([
    ['attachment; filename="options-backtest-run-7-trades.csv"', 'options-backtest-run-7-trades.csv'],
    ['attachment; filename=trades.csv', 'trades.csv'],
    ['attachment; filename=trades.csv; size=10', 'trades.csv'],
    ['ATTACHMENT; FILENAME="Run 7.csv"', 'Run 7.csv'],
  ])('reads %s', (header, name) => {
    expect(filenameOf(header)).toBe(name);
  });

  it.each([
    [null],
    [''],
    ['attachment'],
    ['attachment; filename=""'],
    ['attachment; filename="../../etc/passwd"'],
    ['attachment; filename="a\\b.csv"'],
  ])('gives no name for %s: nothing to trust', (header) => {
    expect(filenameOf(header)).toBeNull();
  });
});

describe('RunDetailService, comparing two runs', () => {
  let service: RunDetailService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(RunDetailService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('asks for run a and run b by their ids', async () => {
    const pending = service.compare(5, 9);
    const request = http.expectOne((req) => req.url === `${BASE}/runs/compare`);
    request.flush({ underlying: 'SPY' });

    expect(request.request.params.get('a')).toBe('5');
    expect(request.request.params.get('b')).toBe('9');
    await expect(pending).resolves.toEqual({ underlying: 'SPY' });
  });

  it.each([
    [409, 'OPTIONS_BACKTEST_RUN_NOT_COMPLETE', 'Only complete runs can be compared, and run 9 (b) is failed.'],
    [409, 'OPTIONS_BACKTEST_RUNS_OF_DIFFERENT_UNDERLYINGS', 'Only runs of one underlying can be compared: run 5 is SPY and run 9 is QQQ.'],
    [409, 'OPTIONS_BACKTEST_RUN_SETUP_CANNOT_BE_READ', 'Runs can be compared only while both stored setups can be read.'],
    [404, 'OPTIONS_BACKTEST_RUN_NOT_FOUND', 'No run has the id 9 (run b).'],
  ])('reads a %s (%s) as a refusal with the server’s code and message', async (status, code, message) => {
    const pending = service.compare(5, 9);
    http.expectOne((req) => req.url === `${BASE}/runs/compare`).flush({ detail: { code, message } }, { status, statusText: 'x' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(RunDetailRefusedError);
    expect(error).toMatchObject({ code, message, status });
  });
});
