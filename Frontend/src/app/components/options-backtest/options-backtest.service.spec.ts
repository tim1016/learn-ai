import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { OptionsBacktestService, SetupInvalidError, SetupRefusedError } from './options-backtest.service';
import { exampleBody, summaryFor } from './testing/fixtures';

const BASE = '/api/research/options-backtest';

describe('OptionsBacktestService', () => {
  let service: OptionsBacktestService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), { provide: JobsService, useValue: {} }],
    });
    service = TestBed.inject(OptionsBacktestService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('reads the session clock from the relative route', async () => {
    const pending = service.sessionClock();
    http.expectOne(`${BASE}/session-clock`).flush({ open_minute_et: 570, close_minute_et: 960 });

    await expect(pending).resolves.toEqual({ open_minute_et: 570, close_minute_et: 960 });
  });

  it('posts the body it is given, untouched, to describe', async () => {
    const pending = service.describe(exampleBody());
    const request = http.expectOne(`${BASE}/setups/describe`);
    request.flush(summaryFor('w'));

    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual(exampleBody());
    await expect(pending).resolves.toMatchObject({ window: ['w window.'], setup: exampleBody() });
  });

  it('turns a 422 into the server reasons, with where each one is', async () => {
    const pending = service.describe({});
    http.expectOne(`${BASE}/setups/describe`).flush(
      { detail: [{ loc: ['body', 'legs', 0, 'contracts'], msg: 'Input should be at most 1000', type: 'x' }, { nonsense: true }] },
      { status: 422, statusText: 'Unprocessable Entity' },
    );

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SetupInvalidError);
    expect(error).toMatchObject({ details: [{ loc: ['body', 'legs', 0, 'contracts'], msg: 'Input should be at most 1000' }] });
  });

  it('still reports a 422 whose body is not in the usual shape', async () => {
    const pending = service.describe({});
    http.expectOne(`${BASE}/setups/describe`).flush('nope', { status: 422, statusText: 'Unprocessable Entity' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SetupInvalidError);
    expect(error).toMatchObject({ details: [{ loc: [], msg: expect.any(String) }] });
  });

  it('lists the saved setups', async () => {
    const pending = service.list();
    http.expectOne(`${BASE}/setups`).flush([{ name: 'a', example: true }]);

    await expect(pending).resolves.toEqual([{ name: 'a', example: true }]);
  });

  it('saves under the name with every character that could change the path escaped', async () => {
    const pending = service.save('SPY 0DTE, a/b (x)', exampleBody());
    const request = http.expectOne(`${BASE}/setups/SPY%200DTE%2C%20a%2Fb%20(x)`);
    request.flush({ name: 'SPY 0DTE, a/b (x)', example: false });

    expect(request.request.method).toBe('PUT');
    expect(request.request.body).toEqual(exampleBody());
    await expect(pending).resolves.toMatchObject({ example: false });
  });

  it('reads a 409 on save as a refusal with the server reason', async () => {
    const pending = service.save('An example', exampleBody());
    http.expectOne(`${BASE}/setups/An%20example`).flush(
      { detail: { code: 'OPTIONS_BACKTEST_SETUP_IS_AN_EXAMPLE', message: "'An example' is one of the shipped examples." } },
      { status: 409, statusText: 'Conflict' },
    );

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SetupRefusedError);
    expect(error).toMatchObject({ code: 'OPTIONS_BACKTEST_SETUP_IS_AN_EXAMPLE', status: 409, message: "'An example' is one of the shipped examples." });
  });

  it('reads a 422 on save as the setup not being valid', async () => {
    const pending = service.save('x', exampleBody());
    http.expectOne(`${BASE}/setups/x`).flush({ detail: [{ loc: ['path', 'name'], msg: 'bad name' }] }, { status: 422, statusText: 'Unprocessable Entity' });

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toMatchObject({ details: [{ loc: ['path', 'name'], msg: 'bad name' }] });
  });

  it('deletes by name and reads a 404 as a refusal', async () => {
    const deleted = service.remove('gone');
    http.expectOne(`${BASE}/setups/gone`).flush(
      { detail: { code: 'OPTIONS_BACKTEST_SETUP_NOT_FOUND', message: "No saved setup is named 'gone'." } },
      { status: 404, statusText: 'Not Found' },
    );

    const error = await deleted.catch((caught: unknown) => caught);
    expect(error).toMatchObject({ code: 'OPTIONS_BACKTEST_SETUP_NOT_FOUND', status: 404 });
  });

  it('resolves a delete the server answers with no content', async () => {
    const deleted = service.remove('mine');
    const request = http.expectOne(`${BASE}/setups/mine`);
    request.flush(null, { status: 204, statusText: 'No Content' });

    expect(request.request.method).toBe('DELETE');
    await expect(deleted).resolves.toBeUndefined();
  });
});
