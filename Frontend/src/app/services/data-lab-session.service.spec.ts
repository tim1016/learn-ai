import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import { DataLabSessionService, type DataLabSessionConfig } from './data-lab-session.service';

const CONFIG: DataLabSessionConfig = {
  ticker: 'SPY',
  fromDate: '2026-08-06',
  toDate: '2026-09-05',
  session: 'rth',
  forwardFill: false,
  adjusted: true,
  entries: [{ name: 'ema', params: { length: 20 } }],
};

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

  it('saves a session with the schema input type DataLabSessionInput and returns the new id', async () => {
    // #1971: the mutation declared `DataLabSessionInputInput!`, which the schema
    // does not define, so every save was refused with HTTP 400.
    const pending = service.saveSession(CONFIG, null, 'bug hunt');

    const req = httpMock.expectOne(environment.backendUrl);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.query).toContain('$input: DataLabSessionInput!');
    expect(req.request.body.variables.input).toMatchObject({
      name: 'bug hunt',
      ticker: 'SPY',
      fromDate: '2026-08-06',
      toDate: '2026-09-05',
      entriesJson: JSON.stringify(CONFIG.entries),
      chartSnapshotJson: null,
    });
    req.flush({ data: { saveDataLabSession: { success: true, id: 'session-1', message: null } } });

    await expect(pending).resolves.toBe('session-1');
  });

  it('updates a session with the same schema input type', async () => {
    const pending = service.updateSession('session-1', CONFIG, null, 'renamed');

    const req = httpMock.expectOne(environment.backendUrl);
    expect(req.request.body.query).toContain('$id: UUID!');
    expect(req.request.body.query).toContain('$input: DataLabSessionInput!');
    expect(req.request.body.variables.id).toBe('session-1');
    req.flush({ data: { updateDataLabSession: { success: true, id: 'session-1', message: null } } });

    await expect(pending).resolves.toBe(true);
  });

  it('rejects instead of reporting a saved session when the server refuses the request', async () => {
    // What #1971 looked like from the browser: a 400 whose body never reaches the caller.
    const pending = service.saveSession(CONFIG, null, 'bug hunt');

    httpMock.expectOne(environment.backendUrl).flush(
      { errors: [{ message: 'The variable `input` is not compatible with the type of the current location.' }] },
      { status: 400, statusText: 'Bad Request' },
    );

    await expect(pending).rejects.toMatchObject({ status: 400 });
  });
});
