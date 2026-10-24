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

  it('is at the strikes box for a window that is too wide, and can be tried again when the catalog is out of reach (503) and not when the request itself is the problem (400)', () => {
    expect.soft(readProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_WINDOW_TOO_WIDE', 'ask for fewer strikes each side', 400), 'unused'), 'is at the strikes box for a window that is too wide').toMatchObject({ control: 'strikes' });
    expect.soft(readProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', 'cannot be reached', 503), 'unused').canRetry, 'can be tried again when the catalog is out of reach (503)').toBe(true);
    expect.soft(readProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_NOT_AN_EXPIRY', 'not an anchor', 400), 'unused').canRetry, 'cannot be tried again when the request itself is the problem (400)').toBe(false);
  });

  it.each([
    [['query', 'strikes'], 'strikes'],
    [['query', 'underlying'], 'underlying'],
    [['query', 'trading_date_ms'], 'date'],
    [['query', 'at_ms'], 'time'],
    [['query', 'expiry_ms'], 'area'],
    [[], 'area'],
    // A parameter name that is also a property of every object is not taken for a control.
    [['query', 'toString'], 'area'],
  ] as const)('puts a 422 about %j at %s', (loc, control) => {
    expect(readProblemOf(invalid([...loc]), 'unused').control).toBe(control);
  });

  it('puts a 422 at the first control it names, and says every reason the server gave', () => {
    const problem = readProblemOf(invalid(['query', 'strikes'], ['query', 'at_ms']), 'unused');

    expect(problem).toEqual({ message: 'wrong at query.strikes wrong at query.at_ms', code: null, control: 'strikes', canRetry: false });
  });

  it('says what the browser says when the call never reached the server and allows another try, and reads a sentence the server sent in a shape of its own', () => {
    expect.soft(readProblemOf(new HttpErrorResponse({ status: 0, statusText: 'Unknown Error' }), 'The listing could not be read.'), 'says what the browser says when the call never reached the server, and allows another try').toEqual({
      message: 'The listing could not be read.',
      code: null,
      control: 'area',
      canRetry: true,
    });
    expect.soft(readProblemOf(new HttpErrorResponse({ status: 500, error: { detail: { message: 'the lake is on fire' } } }), 'fallback').message, 'reads a sentence the server sent in a shape of its own').toBe('the lake is on fire');
  });
});

describe('why a download did not happen', () => {
  it('is that the page is not allowed, or the server refusal with its code, or words of its own when nothing readable came back', () => {
    expect.soft(downloadProblemOf(new DownloadNotAllowedError('The secret does not match.', 403)), 'is that the page is not allowed, with the server own sentence when there is one').toEqual({ kind: 'not_allowed', serverMessage: 'The secret does not match.' });
    expect.soft(downloadProblemOf(new DownloadNotAllowedError(null, 401)), 'is that the page is not allowed, with no sentence when there is none').toEqual({ kind: 'not_allowed', serverMessage: null });
    expect.soft(downloadProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_TOO_MANY_CONTRACTS', 'a download takes at most N contracts', 400)), 'is the server refusal with its code, and says nothing was downloaded for a refusal of the request itself').toEqual({
      kind: 'refused',
      message: 'a download takes at most N contracts',
      code: 'OPTIONS_BACKTEST_TOO_MANY_CONTRACTS',
      nothingDownloaded: true,
    });
    expect.soft(downloadProblemOf(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', 'out of reach', 503)), 'does not say nothing was downloaded for a refusal that is not of the request itself').toMatchObject({ nothingDownloaded: false });
    expect.soft(downloadProblemOf(new HttpErrorResponse({ status: 0 })), 'has words of its own when nothing readable came back, and cannot say that nothing was downloaded').toEqual({ kind: 'refused', message: 'The download could not be started.', code: null, nothingDownloaded: false });
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
});
