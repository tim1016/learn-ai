import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import {
  DataLabSessionService,
  type DataLabSessionChartSnapshot,
  type DataLabSessionConfig,
} from './data-lab-session.service';

const BASE = `${environment.pythonServiceUrl}/api/data-lab/sessions`;
const ID = '6f1f3c0e-0c52-4b0e-9f29-2b6f4a1d9e11';

const CONFIG: DataLabSessionConfig = {
  ticker: 'SPY',
  fromDate: '2026-08-06',
  toDate: '2026-09-05',
  session: 'rth',
  forwardFill: false,
  adjusted: true,
  entries: [{ name: 'ema', params: { length: 20 } }],
};

const SNAPSHOT = { timeframe: '1D', bars: [], indicators: [] } as unknown as DataLabSessionChartSnapshot;

function row(overrides: Record<string, unknown> = {}) {
  return {
    id: ID,
    name: 'bug hunt',
    ticker: 'SPY',
    from_date: '2026-08-06',
    to_date: '2026-09-05',
    session: 'rth',
    forward_fill: false,
    adjusted: true,
    entries: [{ name: 'ema', params: { length: 20 } }],
    chart_snapshot: null,
    window_start_ms: 1_785_974_400_000,
    window_end_ms: 1_788_566_400_000,
    created_at_ms: 1_790_000_000_000,
    updated_at_ms: 1_790_000_500_000,
    ...overrides,
  };
}

const notFound = { detail: { code: 'DATA_LAB_SESSION_NOT_FOUND', message: `Session ${ID} not found` } };

describe('DataLabSessionService', () => {
  let service: DataLabSessionService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(DataLabSessionService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('lists sessions as summaries carrying the server-stamped instants', async () => {
    const pending = service.listSessions();

    const req = httpMock.expectOne(BASE);
    expect(req.request.method).toBe('GET');
    req.flush([row(), row({ id: 'other', entries: [], chart_snapshot: { timeframe: '1D' }, updated_at_ms: 1_790_000_900_000 })]);

    await expect(pending).resolves.toEqual([
      {
        id: ID, name: 'bug hunt', ticker: 'SPY', fromDate: '2026-08-06', toDate: '2026-09-05',
        createdAtMs: 1_790_000_000_000, updatedAtMs: 1_790_000_500_000, indicatorCount: 1, hasChart: false,
      },
      {
        id: 'other', name: 'bug hunt', ticker: 'SPY', fromDate: '2026-08-06', toDate: '2026-09-05',
        createdAtMs: 1_790_000_000_000, updatedAtMs: 1_790_000_900_000, indicatorCount: 0, hasChart: true,
      },
    ]);
  });

  it('says why the list could not load when the store is unreachable', async () => {
    const pending = service.listSessions();

    httpMock.expectOne(BASE).flush(
      { detail: { code: 'DATA_LAB_SESSIONS_STORE_UNAVAILABLE', message: 'The saved Data Lab sessions could not be reached.' } },
      { status: 503, statusText: 'Service Unavailable' },
    );

    await expect(pending).rejects.toThrow('Could not load the saved setups: The saved Data Lab sessions could not be reached.');
  });

  it('gets one session whole, with its entries and chart restored as the screen saved them', async () => {
    const pending = service.getSession(ID);

    const req = httpMock.expectOne(`${BASE}/${ID}`);
    expect(req.request.method).toBe('GET');
    req.flush(row({ session: 'extended', adjusted: false, chart_snapshot: { timeframe: '5m' } }));

    await expect(pending).resolves.toEqual({
      id: ID,
      name: 'bug hunt',
      createdAtMs: 1_790_000_000_000,
      updatedAtMs: 1_790_000_500_000,
      config: { ...CONFIG, session: 'extended', adjusted: false },
      chartSnapshot: { timeframe: '5m' },
    });
  });

  it('resolves undefined for a session nobody holds', async () => {
    const pending = service.getSession(ID);

    httpMock.expectOne(`${BASE}/${ID}`).flush(notFound, { status: 404, statusText: 'Not Found' });

    await expect(pending).resolves.toBeUndefined();
  });

  it('does not mistake a missing route for a missing session', async () => {
    const pending = service.getSession(ID);

    httpMock.expectOne(`${BASE}/${ID}`).flush({ detail: 'Not Found' }, { status: 404, statusText: 'Not Found' });

    await expect(pending).rejects.toThrow('Could not load the setup: Not Found');
  });

  it('saves a new session as snake_case JSON documents and returns the new id', async () => {
    const pending = service.saveSession(CONFIG, SNAPSHOT, 'bug hunt');

    const req = httpMock.expectOne(BASE);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      name: 'bug hunt',
      ticker: 'SPY',
      from_date: '2026-08-06',
      to_date: '2026-09-05',
      session: 'rth',
      forward_fill: false,
      adjusted: true,
      entries: CONFIG.entries,
      chart_snapshot: SNAPSHOT,
    });
    req.flush({ id: ID, created_at_ms: 1_790_000_000_000, updated_at_ms: 1_790_000_000_000 }, { status: 201, statusText: 'Created' });

    await expect(pending).resolves.toBe(ID);
  });

  it('names an unnamed session after its ticker and window, and saves no chart as null', async () => {
    const pending = service.saveSession(CONFIG, null);

    const req = httpMock.expectOne(BASE);
    expect(req.request.body).toMatchObject({ name: 'SPY 2026-08-06 → 2026-09-05', chart_snapshot: null });
    req.flush({ id: ID, created_at_ms: 1, updated_at_ms: 1 }, { status: 201, statusText: 'Created' });

    await expect(pending).resolves.toBe(ID);
  });

  it('rejects with the refusal sentence when Python will not take the body', async () => {
    const pending = service.saveSession(CONFIG, null, 'bug hunt');

    httpMock.expectOne(BASE).flush(
      { detail: [{ msg: 'must be a real calendar date written YYYY-MM-DD' }] },
      { status: 422, statusText: 'Unprocessable Entity' },
    );

    await expect(pending).rejects.toThrow('Could not save the setup: must be a real calendar date written YYYY-MM-DD');
  });

  it('replaces a session whole with PUT', async () => {
    const pending = service.updateSession(ID, CONFIG, null, 'renamed');

    const req = httpMock.expectOne(`${BASE}/${ID}`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toMatchObject({ name: 'renamed', ticker: 'SPY', entries: CONFIG.entries, chart_snapshot: null });
    req.flush({ id: ID, created_at_ms: 1, updated_at_ms: 2 });

    await expect(pending).resolves.toBeUndefined();
  });

  it('rejects with Python\'s sentence when the session to replace is gone', async () => {
    const pending = service.updateSession(ID, CONFIG, null, 'renamed');

    httpMock.expectOne(`${BASE}/${ID}`).flush(notFound, { status: 404, statusText: 'Not Found' });

    await expect(pending).rejects.toThrow(`Could not update the setup: Session ${ID} not found`);
  });

  it('renames a session with PATCH on its name', async () => {
    const pending = service.renameSession(ID, 'new name');

    const req = httpMock.expectOne(`${BASE}/${ID}/name`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ name: 'new name' });
    req.flush({ id: ID, created_at_ms: 1, updated_at_ms: 2 });

    await expect(pending).resolves.toBeUndefined();
  });

  it('deletes a session with DELETE and accepts the empty 204', async () => {
    const pending = service.deleteSession(ID);

    const req = httpMock.expectOne(`${BASE}/${ID}`);
    expect(req.request.method).toBe('DELETE');
    req.flush(null, { status: 204, statusText: 'No Content' });

    await expect(pending).resolves.toBeUndefined();
  });

  it('rejects a delete the server refused', async () => {
    const pending = service.deleteSession(ID);

    httpMock.expectOne(`${BASE}/${ID}`).flush(notFound, { status: 404, statusText: 'Not Found' });

    await expect(pending).rejects.toThrow(`Could not delete the setup: Session ${ID} not found`);
  });
});
