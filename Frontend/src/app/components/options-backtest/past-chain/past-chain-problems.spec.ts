import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';

import { downloadProblemOf, NOT_A_SESSION, readProblemOf } from './past-chain-problems';
import { DownloadNotAllowedError, PastChainInvalidError, PastChainRefusedError } from './past-chain.service';

const invalid = (...locs: (string | number)[][]) => new PastChainInvalidError(locs.map((loc) => ({ loc, msg: `wrong at ${loc.join('.')}` })));

describe('where a refusal of a read is said', () => {
  it('is in the area that was being read, in the server sentence, for a refusal about the date', () => {
    const problem = readProblemOf(new PastChainRefusedError(NOT_A_SESSION, '2026-10-10 is a weekend day', 400), 'unused');

    expect(problem).toEqual({ message: '2026-10-10 is a weekend day', code: 'OPTIONS_BACKTEST_NOT_A_SESSION', control: 'area', canRetry: false });
  });

  it.each(['OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION', 'OPTIONS_BACKTEST_INSTANT_NOT_ON_A_MINUTE'])('is at the time box for %s', (code) => {
    expect(readProblemOf(new PastChainRefusedError(code, 'the time is wrong', 400), 'unused')).toMatchObject({ control: 'time', message: 'the time is wrong', canRetry: false });
  });

  it('is at the strikes box for a window that is too wide', () => {
    expect(readProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_WINDOW_TOO_WIDE', 'ask for fewer strikes each side', 400), 'unused')).toMatchObject({ control: 'strikes' });
  });

  it('can be tried again when the catalog is out of reach (503) and not when the request itself is the problem (400)', () => {
    expect(readProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', 'cannot be reached', 503), 'unused').canRetry).toBe(true);
    expect(readProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_NOT_AN_EXPIRY', 'not an anchor', 400), 'unused').canRetry).toBe(false);
  });

  it.each([
    [['query', 'strikes'], 'strikes'],
    [['query', 'underlying'], 'underlying'],
    [['query', 'trading_date_ms'], 'date'],
    [['query', 'at_ms'], 'time'],
    [['query', 'expiry_ms'], 'area'],
    [[], 'area'],
  ] as const)('puts a 422 about %j at %s', (loc, control) => {
    expect(readProblemOf(invalid([...loc]), 'unused').control).toBe(control);
  });

  it('puts a 422 at the first control it names, and says every reason the server gave', () => {
    const problem = readProblemOf(invalid(['query', 'strikes'], ['query', 'at_ms']), 'unused');

    expect(problem).toEqual({ message: 'wrong at query.strikes wrong at query.at_ms', code: null, control: 'strikes', canRetry: false });
  });

  it('does not take a parameter name that is also a property of every object for a control', () => {
    expect(readProblemOf(invalid(['query', 'toString']), 'unused').control).toBe('area');
  });

  it('says what the browser says when the call never reached the server, and allows another try', () => {
    const problem = readProblemOf(new HttpErrorResponse({ status: 0, statusText: 'Unknown Error' }), 'The listing could not be read.');

    expect(problem).toEqual({ message: 'The listing could not be read.', code: null, control: 'area', canRetry: true });
  });

  it('reads a sentence the server sent in a shape of its own', () => {
    const problem = readProblemOf(new HttpErrorResponse({ status: 500, error: { detail: { message: 'the lake is on fire' } } }), 'fallback');

    expect(problem.message).toBe('the lake is on fire');
  });
});

describe('why a download did not happen', () => {
  it('is that the page is not allowed, with the server own sentence when there is one', () => {
    expect(downloadProblemOf(new DownloadNotAllowedError('The secret does not match.', 403))).toEqual({ kind: 'not_allowed', serverMessage: 'The secret does not match.' });
    expect(downloadProblemOf(new DownloadNotAllowedError(null, 401))).toEqual({ kind: 'not_allowed', serverMessage: null });
  });

  it('is the server refusal with its code, and says nothing was downloaded only for a refusal of the request itself', () => {
    expect(downloadProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_TOO_MANY_CONTRACTS', 'a download takes at most N contracts', 400))).toEqual({
      kind: 'refused',
      message: 'a download takes at most N contracts',
      code: 'OPTIONS_BACKTEST_TOO_MANY_CONTRACTS',
      nothingDownloaded: true,
    });
    expect(downloadProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', 'out of reach', 503))).toMatchObject({ nothingDownloaded: false });
  });

  it.each([
    ['a 422 the server read and refused', invalid(['body', 'underlying']), true],
    ['a 404', new HttpErrorResponse({ status: 404 }), true],
    ['a 409', new HttpErrorResponse({ status: 409 }), true],
    ['a 500', new HttpErrorResponse({ status: 500, error: { detail: { message: 'the lake is on fire' } } }), false],
    ['a 502', new HttpErrorResponse({ status: 502 }), false],
    ['a call that never came back', new HttpErrorResponse({ status: 0 }), false],
    ['something that is not an answer at all', new Error('network down'), false],
  ])('says nothing was downloaded only when the server refused before doing anything: %s is %s', (_name, error, nothing) => {
    expect(downloadProblemOf(error)).toMatchObject({ kind: 'refused', nothingDownloaded: nothing });
  });

  it('has words of its own when nothing readable came back, and cannot say that nothing was downloaded', () => {
    expect(downloadProblemOf(new HttpErrorResponse({ status: 0 }))).toEqual({ kind: 'refused', message: 'The download could not be started.', code: null, nothingDownloaded: false });
  });
});
