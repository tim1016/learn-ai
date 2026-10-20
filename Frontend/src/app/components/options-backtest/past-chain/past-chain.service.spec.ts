import { HttpErrorResponse, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { dataPlaneControlIntentInterceptor } from '../../../security/data-plane-control-intent.interceptor';
import { DownloadNotAllowedError, PastChainInvalidError, PastChainRefusedError, PastChainService, sameExpiryQuery, type ExpiryQuery } from './past-chain.service';
import { chainDay, chainExpiry, downloadAnswer, EXPIRY_MS, TRADING_DATE_MS } from './testing/fixtures';

const BASE = '/api/research/options-backtest/past-chain';
const REQUEST = { underlying: 'SPY', trading_date_ms: TRADING_DATE_MS, expiry_ms: EXPIRY_MS['2026-10-16'], contracts: ['SPY261016C00602000'] };

describe('PastChainService', () => {
  let service: PastChainService;
  let http: HttpTestingController;

  beforeEach(() => {
    // The interceptor the app registers is in the chain, as in the app: it is what makes the guarded call a control call.
    TestBed.configureTestingModule({ providers: [provideHttpClient(withInterceptors([dataPlaneControlIntentInterceptor])), provideHttpClientTesting()] });
    service = TestBed.inject(PastChainService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  describe('the day', () => {
    it('asks for the underlying and the trading date as New York midnight, and nothing else, and sends no secret and no intent', async () => {
      const pending = service.day({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS });
      const request = http.expectOne((each) => each.url === `${BASE}/day`);
      request.flush(chainDay());

      expect.soft(request.request.method, 'asks for the underlying and the trading date as New York midnight, and nothing else (the method)').toBe('GET');
      expect.soft(request.request.params.keys().sort(), 'asks for the underlying and the trading date as New York midnight, and nothing else (the parameters)').toEqual(['trading_date_ms', 'underlying']);
      expect.soft(request.request.params.get('underlying'), 'asks for the underlying and the trading date as New York midnight, and nothing else (the underlying)').toBe('SPY');
      expect.soft(request.request.params.get('trading_date_ms'), 'asks for the underlying and the trading date as New York midnight, and nothing else (the date)').toBe('1791518400000');
      await expect.soft(pending, 'asks for the underlying and the trading date as New York midnight, and nothing else (the answer)').resolves.toEqual(chainDay());
      expect.soft(request.request.headers.has('X-Data-Plane-Control-Secret'), 'sends no secret and no intent: a read is open (no secret)').toBe(false);
      expect.soft(request.request.headers.has('X-Data-Plane-Control-Intent'), 'sends no secret and no intent: a read is open (no intent)').toBe(false);
    });
  });

  describe('one expiry', () => {
    it('asks with the expiry anchor as served, and leaves out the instant and the strikes the owner did not choose', async () => {
      const pending = service.expiry({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, expiryMs: EXPIRY_MS['2026-10-16'], atMs: null, strikes: null });
      const request = http.expectOne((each) => each.url === `${BASE}/expiry`);
      request.flush(chainExpiry());

      expect(request.request.params.keys().sort()).toEqual(['expiry_ms', 'trading_date_ms', 'underlying']);
      expect(request.request.params.get('expiry_ms')).toBe('1792180800000');
      await expect(pending).resolves.toEqual(chainExpiry());
    });

    it('sends the instant and the strikes when the owner chose them, the strikes as the digits typed', async () => {
      const pending = service.expiry({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, expiryMs: EXPIRY_MS['2026-10-16'], atMs: 1_791_575_100_000, strikes: '14' });
      const request = http.expectOne((each) => each.url === `${BASE}/expiry`);
      request.flush(chainExpiry());
      await pending;

      expect(request.request.params.get('at_ms')).toBe('1791575100000');
      expect(request.request.params.get('strikes')).toBe('14');
    });

    it('sends zero strikes, which is a choice and not the same as none', async () => {
      const pending = service.expiry({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, expiryMs: EXPIRY_MS['2026-10-16'], atMs: null, strikes: '0' });
      const request = http.expectOne((each) => each.url === `${BASE}/expiry`);
      request.flush(chainExpiry());
      await pending;

      expect(request.request.params.get('strikes')).toBe('0');
    });
  });

  describe('the download', () => {
    it('posts exactly the body it is given, to the relative route, marked as the browser own control call by the app interceptor, and never sends the secret itself', async () => {
      const pending = service.download(REQUEST);
      const request = http.expectOne(`${BASE}/download`);
      request.flush(downloadAnswer([]));

      expect.soft(request.request.method, 'posts exactly the body it is given, to the relative route (the method)').toBe('POST');
      expect.soft(request.request.body, 'posts exactly the body it is given, to the relative route (the body)').toEqual(REQUEST);
      await expect.soft(pending, 'posts exactly the body it is given, to the relative route (the answer)').resolves.toEqual(downloadAnswer([]));
      expect.soft(request.request.headers.get('X-Data-Plane-Control-Intent'), 'is marked as the browser own control call by the app interceptor, and the page never sends the secret itself (the intent)').toBe('learn-ai-browser-control');
      expect.soft(request.request.headers.has('X-Data-Plane-Control-Secret'), 'is marked as the browser own control call by the app interceptor, and the page never sends the secret itself (no secret)').toBe(false);
    });

    it.each([401, 403])('says the page is not allowed when the data plane answers %i, with the server sentence when there is one', async (status) => {
      const pending = service.download(REQUEST);
      http.expectOne(`${BASE}/download`).flush(
        { reason: 'data_plane_control_secret_refused', message: 'The presented secret does not match.', next_step: 'Check it.' },
        { status, statusText: 'Refused' },
      );

      const error = await pending.catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(DownloadNotAllowedError);
      expect(error).toMatchObject({ serverMessage: 'The presented secret does not match.', status });
    });

    it('says the page is not allowed when the refusal has no sentence at all', async () => {
      const pending = service.download(REQUEST);
      http.expectOne(`${BASE}/download`).flush('', { status: 403, statusText: 'Forbidden' });

      const error = await pending.catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(DownloadNotAllowedError);
      expect(error).toMatchObject({ serverMessage: null });
    });
  });

  describe('refusals', () => {
    it.each([
      [
        'keeps the code and the sentence of a 400, and the status it came with',
        () => service.day({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS }),
        `${BASE}/day`,
        { detail: { code: 'OPTIONS_BACKTEST_NOT_A_SESSION', message: '2026-10-10 is a weekend day: the exchange had no session that day' } },
        { status: 400, statusText: 'Bad Request' },
        PastChainRefusedError,
        { code: 'OPTIONS_BACKTEST_NOT_A_SESSION', message: '2026-10-10 is a weekend day: the exchange had no session that day', status: 400 },
      ],
      [
        'reads the data plane flat refusal of a download too (a 503 that names a reason and no code)',
        () => service.download(REQUEST),
        `${BASE}/download`,
        { reason: 'fleet_control_plane_not_installed', message: 'A required control-plane component is not configured.', next_step: 'Needs the host operator.' },
        { status: 503, statusText: 'Service Unavailable' },
        PastChainRefusedError,
        { code: 'fleet_control_plane_not_installed', message: 'A required control-plane component is not configured.', status: 503 },
      ],
      [
        'turns a 422 into what is wrong and where, in the server words',
        () => service.expiry({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, expiryMs: EXPIRY_MS['2026-10-16'], atMs: null, strikes: '15' }),
        `${BASE}/expiry`,
        { detail: [{ loc: ['query', 'strikes'], msg: 'Input should be less than or equal to 14', type: 'less_than_equal' }, { nonsense: true }] },
        { status: 422, statusText: 'Unprocessable Entity' },
        PastChainInvalidError,
        { details: [{ loc: ['query', 'strikes'], msg: 'Input should be less than or equal to 14' }] },
      ],
      [
        'still reports a 422 whose body is not in the usual shape',
        () => service.day({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS }),
        `${BASE}/day`,
        'nope',
        { status: 422, statusText: 'Unprocessable Entity' },
        PastChainInvalidError,
        { details: [{ loc: [], msg: expect.any(String) }] },
      ],
      [
        'lets a failure that is not the server own refusal through as it came',
        () => service.day({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS }),
        `${BASE}/day`,
        'boom',
        { status: 500, statusText: 'Server Error' },
        HttpErrorResponse,
        { status: 500 },
      ],
    ])('%s', async (_name, ask, url, body, init, errorClass, match) => {
      const pending: Promise<unknown> = ask();
      http.expectOne((each) => each.url === url).flush(body, init);

      const error = await pending.catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(errorClass);
      expect(error).toMatchObject(match);
    });

    it('keeps the code and the sentence of the catalog being out of reach (503)', async () => {
      const pending = service.expiry({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, expiryMs: EXPIRY_MS['2026-10-16'], atMs: null, strikes: null });
      http.expectOne((each) => each.url === `${BASE}/expiry`).flush(
        { detail: { code: 'OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', message: "The data lake's catalog cannot be reached, so what is stored cannot be read." } },
        { status: 503, statusText: 'Service Unavailable' },
      );

      const error = await pending.catch((caught: unknown) => caught);
      expect(error).toMatchObject({ code: 'OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', status: 503 });
    });

    it('does not take a 403 of a read for the download not being allowed', async () => {
      const pending = service.day({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS });
      http.expectOne((each) => each.url === `${BASE}/day`).flush({ reason: 'x', message: 'y' }, { status: 403, statusText: 'Forbidden' });

      const error = await pending.catch((caught: unknown) => caught);
      expect(error).not.toBeInstanceOf(DownloadNotAllowedError);
    });
  });
});

describe('whether two questions about a chain are the same one', () => {
  const asked: ExpiryQuery = { underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, expiryMs: EXPIRY_MS['2026-10-16'], atMs: null, strikes: null };

  it('is so for another object that says the same and for two questions not asked at all, and not so for a question and none', () => {
    expect.soft(sameExpiryQuery(asked, { ...asked }), 'is so for another object that says the same, and for two questions not asked at all (1 of 2)').toBe(true);
    expect.soft(sameExpiryQuery(undefined, undefined), 'is so for another object that says the same, and for two questions not asked at all (2 of 2)').toBe(true);

    expect.soft(sameExpiryQuery(asked, undefined), 'is not so for a question and none (1 of 2)').toBe(false);
    expect.soft(sameExpiryQuery(undefined, asked), 'is not so for a question and none (2 of 2)').toBe(false);
  });

  it.each([
    ['underlying', { underlying: 'AAPL' }],
    ['date', { tradingDateMs: TRADING_DATE_MS + 86_400_000 }],
    ['expiry', { expiryMs: EXPIRY_MS['2026-10-30'] }],
    ['time', { atMs: TRADING_DATE_MS + 60_000 }],
    ['strikes', { strikes: '3' }],
  ])('is not so when the %s differs', (_name, changes) => {
    expect(sameExpiryQuery(asked, { ...asked, ...changes })).toBe(false);
  });
});
