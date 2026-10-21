import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import { isPythonRefusal, pythonFailureMessage } from './python-http-failure';

function failure(status: number, error: unknown): HttpErrorResponse {
  return new HttpErrorResponse({ status, statusText: 'Failure', url: '/api/x', error });
}

describe('pythonFailureMessage', () => {
  it.each<[string, unknown, string]>([
    ['a string detail', { detail: 'Polygon rate limited' }, 'Polygon rate limited'],
    ['a { code, message } detail', { detail: { code: 'X', message: 'Row 7 not found' } }, 'Row 7 not found'],
    [
      'every reason of a 422 validation list, joined',
      { detail: [{ loc: ['body', 'a'], msg: 'too small' }, { msg: 'too long' }, { loc: ['body'] }] },
      'too small; too long',
    ],
    ['the fleet\'s flat body', { reason: 'clerk_unreachable', message: 'The clerk did not answer.' }, 'The clerk did not answer.'],
  ])('reads %s', (_name, body, sentence) => {
    expect(pythonFailureMessage(failure(422, body))).toBe(sentence);
  });

  it.each<[string, unknown]>([
    ['an empty detail', { detail: '' }],
    ['a validation list with no reasons', { detail: [{ loc: ['body'] }] }],
    ['no body', null],
  ])('falls back to the transport message for %s', (_name, body) => {
    const failed = failure(500, body);

    expect(pythonFailureMessage(failed)).toBe(failed.message);
  });
});

describe('isPythonRefusal', () => {
  const body = { detail: { code: 'SESSION_NOT_FOUND', message: 'No such session' } };

  it('is true for the named refusal at its status', () => {
    expect(isPythonRefusal(failure(404, body), 404, 'SESSION_NOT_FOUND')).toBe(true);
  });

  it.each<[string, unknown]>([
    ['a bare 404 from a route the server does not have', failure(404, { detail: 'Not Found' })],
    ['another code at the same status', failure(404, { detail: { code: 'OTHER', message: 'x' } })],
    ['the same code at another status', failure(409, body)],
    ['a failure that is not an HTTP one', new Error('boom')],
  ])('is false for %s', (_name, failed) => {
    expect(isPythonRefusal(failed, 404, 'SESSION_NOT_FOUND')).toBe(false);
  });
});
